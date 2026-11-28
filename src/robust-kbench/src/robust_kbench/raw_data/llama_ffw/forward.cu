#include <cuda_runtime.h>
#include <torch/extension.h>
#include <ATen/cuda/CUDAContext.h>
#include <c10/cuda/CUDAGuard.h>

// Matrix multiplication kernel with double precision accumulation
__global__ void matmul_kernel(
    const float* __restrict__ A,  // [M, K]
    const float* __restrict__ B,  // [K, N]
    float* __restrict__ C,        // [M, N]
    const int M, 
    const int K, 
    const int N
) {
    const int row = blockIdx.y * blockDim.y + threadIdx.y;
    const int col = blockIdx.x * blockDim.x + threadIdx.x;
    
    if (row < M && col < N) {
        double sum = 0.0;
        for (int k = 0; k < K; ++k) {
            sum += (double)A[row * K + k] * (double)B[k * N + col];
        }
        C[row * N + col] = (float)sum;
    }
}

// SiLU activation kernel
__global__ void silu_kernel(
    const float* __restrict__ input,
    float* __restrict__ output,
    const int size
) {
    const int idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx < size) {
        const float x = input[idx];
        output[idx] = x / (1.0f + expf(-x));
    }
}

// Element-wise multiplication kernel
__global__ void multiply_kernel(
    const float* __restrict__ a,
    const float* __restrict__ b,
    float* __restrict__ output,
    const int size
) {
    const int idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx < size) {
        output[idx] = a[idx] * b[idx];
    }
}

torch::Tensor ffn_cuda(
    const torch::Tensor& input,
    const torch::Tensor& gate_proj,
    const torch::Tensor& up_proj,
    const torch::Tensor& down_proj
) {
    TORCH_CHECK(input.is_cuda(), "Input must be a CUDA tensor");
    TORCH_CHECK(input.dim() == 3, "Input must be 3D tensor");
    TORCH_CHECK(input.scalar_type() == torch::kFloat32, "Input must be float32");
    
    const int batch = input.size(0);
    const int seq_len = input.size(1);
    const int hidden_size = input.size(2);
    const int intermediate_size = gate_proj.size(0);
    const int batch_seq = batch * seq_len;
    
    // Validate shapes
    TORCH_CHECK(gate_proj.size(1) == hidden_size, "gate_proj has wrong shape");
    TORCH_CHECK(up_proj.size(1) == hidden_size, "up_proj has wrong shape");
    TORCH_CHECK(up_proj.size(0) == intermediate_size, "up_proj has wrong shape");
    TORCH_CHECK(down_proj.size(0) == hidden_size, "down_proj has wrong shape");
    TORCH_CHECK(down_proj.size(1) == intermediate_size, "down_proj has wrong shape");
    
    // Reshape input to 2D
    auto input_2d = input.view({batch_seq, hidden_size});
    
    // Device guard
    const at::cuda::CUDAGuard device_guard(input.device());
    cudaStream_t stream = at::cuda::getCurrentCUDAStream();

    // Allocate intermediate tensors
    auto options = torch::TensorOptions().dtype(torch::kFloat32).device(input.device());
    auto gate_out = torch::empty({batch_seq, intermediate_size}, options);
    auto up_out = torch::empty({batch_seq, intermediate_size}, options);
    auto gate_activated = torch::empty({batch_seq, intermediate_size}, options);
    auto intermediate = torch::empty({batch_seq, intermediate_size}, options);
    auto output_2d = torch::empty({batch_seq, hidden_size}, options);

    // Kernel configurations
    const int BLOCK_SIZE = 16;
    dim3 threads_matmul(BLOCK_SIZE, BLOCK_SIZE);
    
    dim3 grid_up_gate(
        (intermediate_size + BLOCK_SIZE - 1) / BLOCK_SIZE,
        (batch_seq + BLOCK_SIZE - 1) / BLOCK_SIZE
    );
    
    dim3 grid_down(
        (hidden_size + BLOCK_SIZE - 1) / BLOCK_SIZE,
        (batch_seq + BLOCK_SIZE - 1) / BLOCK_SIZE
    );

    const int threads_elem = 256;
    const int elements = batch_seq * intermediate_size;
    const int grid_elem = (elements + threads_elem - 1) / threads_elem;

    // 1. Gate projection
    matmul_kernel<<<grid_up_gate, threads_matmul, 0, stream>>>(
        input_2d.data_ptr<float>(),
        gate_proj.t().contiguous().data_ptr<float>(),
        gate_out.data_ptr<float>(),
        batch_seq, hidden_size, intermediate_size
    );

    // 2. Up projection
    matmul_kernel<<<grid_up_gate, threads_matmul, 0, stream>>>(
        input_2d.data_ptr<float>(),
        up_proj.t().contiguous().data_ptr<float>(),
        up_out.data_ptr<float>(),
        batch_seq, hidden_size, intermediate_size
    );

    // 3. SiLU activation
    silu_kernel<<<grid_elem, threads_elem, 0, stream>>>(
        gate_out.data_ptr<float>(),
        gate_activated.data_ptr<float>(),
        elements
    );

    // 4. Multiply gate and up
    multiply_kernel<<<grid_elem, threads_elem, 0, stream>>>(
        gate_activated.data_ptr<float>(),
        up_out.data_ptr<float>(),
        intermediate.data_ptr<float>(),
        elements
    );

    // 5. Down projection
    matmul_kernel<<<grid_down, threads_matmul, 0, stream>>>(
        intermediate.data_ptr<float>(),
        down_proj.t().contiguous().data_ptr<float>(),
        output_2d.data_ptr<float>(),
        batch_seq, intermediate_size, hidden_size
    );

    return output_2d.view({batch, seq_len, hidden_size});
}

torch::Tensor forward(
    const torch::Tensor& input,
    const torch::Tensor& gate_proj,
    const torch::Tensor& up_proj,
    const torch::Tensor& down_proj
) {
    return ffn_cuda(input, gate_proj, up_proj, down_proj);
}

PYBIND11_MODULE(TORCH_EXTENSION_NAME, m) {
    m.def("forward", &forward, "FFN forward (CUDA)",
          py::arg("input"),
          py::arg("gate_proj"),
          py::arg("up_proj"),
          py::arg("down_proj"));
}
