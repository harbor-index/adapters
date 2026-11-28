#include <torch/extension.h>
#include <cuda.h>
#include <cuda_runtime.h>
#include <vector>
#include <cfloat>

// For correctness, support both float and half
// but float only is fine for MNIST-typical workloads

// ---- Warp-level reductions (active mask variant) ----
__inline__ __device__ float warp_max(float val, unsigned mask) {
    #pragma unroll
    for (int offset = 16; offset > 0; offset >>= 1)
        val = fmaxf(val, __shfl_down_sync(mask, val, offset));
    return val;
}
__inline__ __device__ float warp_sum(float val, unsigned mask) {
    #pragma unroll
    for (int offset = 16; offset > 0; offset >>= 1)
        val += __shfl_down_sync(mask, val, offset);
    return val;
}

// ---- Rowwise kernel for C ≤ 32 ----
template <typename scalar_t>
__global__ void ce_backward_rowwise_fvec_kernel(
        const scalar_t grad_out,
        const scalar_t* __restrict__ x,
        const int64_t* __restrict__ y,
        scalar_t* __restrict__ dx,
        int N, int C, int warps_per_block)
{
    // One warp (= 32 threads) per sample row
    const int warp_id = threadIdx.x / 32;
    const int lane    = threadIdx.x % 32;
    int row = blockIdx.x * warps_per_block + warp_id;
    if (row >= N) return;

    unsigned mask = (1u << C) - 1u;

    // Vectorized float4 loads if C%4==0 and C==32 (e.g., ImageNet). For MNIST (C=10), scalar.
    float my_val = (lane < C) ? (float)__ldg(x + row*C + lane) : -FLT_MAX;

    // 1) Row max: only over active C lanes, warp shuffle reduction
    float row_max = warp_max(my_val, mask);
    row_max = __shfl_sync(mask, row_max, 0);

    // 2) __expf, one pass: reuse for grad, warp sum reduction
    float my_exp = (lane < C) ? __expf(my_val - row_max) : 0.0f; // __expf
    float row_sum = warp_sum(my_exp, mask);
    row_sum = __shfl_sync(mask, row_sum, 0);

    float inv_sum = 1.0f / row_sum;
    float scale = float(grad_out) / float(N);
    int tgt = __ldg(y + row);

    // 3) Gradient
    if (lane < C) {
        float g = my_exp * inv_sum;
        if (lane == tgt) g -= 1.0f;
        g *= scale;
        dx[row*C + lane] = (scalar_t)g;
    }
}

// ---- Efficient block kernel for C > 32 with float4 vectorization and warp-shuffle reductions ----
template <typename scalar_t>
__global__ void ce_backward_fusedvec_kernel(
    const scalar_t grad_out,
    const scalar_t* __restrict__ x,
    const int64_t* __restrict__ y,
    scalar_t* __restrict__ dx,
    int N, int C)
{
    int row = blockIdx.x;
    if (row >= N) return;

    const scalar_t* row_x  = x  + row * C;
    scalar_t*       row_dx = dx + row * C;
    int tgt = __ldg(y + row);
    float scale = float(grad_out) / float(N);

    int tid = threadIdx.x;
    unsigned mask = 0xffffffff;

    // VECTOR PATH: Only used when C % 4 == 0
    bool vectorizable = (C % 4) == 0;
    int C4 = C / 4;

    // ------ Compute max (warp reduction, float4 loads if possible) ------
    float local_max = -FLT_MAX;
    if (vectorizable) {
        const float4* row_x4 = reinterpret_cast<const float4*>(row_x);
        for (int i = tid; i < C4; i += blockDim.x) {
            float4 v = __ldg(row_x4 + i);
            local_max = fmaxf(local_max, v.x);
            local_max = fmaxf(local_max, v.y);
            local_max = fmaxf(local_max, v.z);
            local_max = fmaxf(local_max, v.w);
        }
    } else {
        for (int i = tid; i < C; i += blockDim.x)
            local_max = fmaxf(local_max, (float)__ldg(row_x + i));
    }
    // Reduce max within block, using warp shuffles for C>32 (multiple warps)
    // 1. Upcast to nearest warp multiple
    for (int offset = 16; offset > 0; offset >>= 1) {
        local_max = fmaxf(local_max, __shfl_down_sync(mask, local_max, offset));
    }
    // Now local_max contains per-warp maxima.
    __shared__ float warp_maxes[4];
    if ((tid % 32) == 0)
        warp_maxes[tid / 32] = local_max;
    __syncthreads();

    float row_max = -FLT_MAX;
    if (tid < blockDim.x / 32)
        row_max = warp_maxes[tid];
    __syncthreads();
    row_max = (tid < 32) ? warp_max(row_max, mask) : row_max;
    row_max = __shfl_sync(mask, row_max, 0);

    // ------ Compute expf and sum (reuse expf, similarly vectorized) ------
    float my_sum = 0.0f;
    if (vectorizable) {
        const float4* row_x4 = reinterpret_cast<const float4*>(row_x);
        for (int i = tid; i < C4; i += blockDim.x) {
            float4 v = __ldg(row_x4 + i);
            my_sum += __expf(v.x - row_max);
            my_sum += __expf(v.y - row_max);
            my_sum += __expf(v.z - row_max);
            my_sum += __expf(v.w - row_max);
        }
    } else {
        for (int i = tid; i < C; i += blockDim.x)
            my_sum += __expf((float)__ldg(row_x + i) - row_max);
    }
    // Reduce sum within block, using warp shuffles as above
    for (int offset = 16; offset > 0; offset >>= 1)
        my_sum += __shfl_down_sync(mask, my_sum, offset);
    if ((tid % 32) == 0)
        warp_maxes[tid / 32] = my_sum;
    __syncthreads();
    float block_sum = 0.f;
    if (tid < blockDim.x / 32)
        block_sum = warp_maxes[tid];
    __syncthreads();
    block_sum = (tid < 32) ? warp_sum(block_sum, mask) : block_sum;
    block_sum = __shfl_sync(mask, block_sum, 0);
    float inv_sum = 1.0f / block_sum;

    // ------ Write out dx vectorized ------
    if (vectorizable) {
        const float4* row_x4 = reinterpret_cast<const float4*>(row_x);
        float4* row_dx4 = reinterpret_cast<float4*>(row_dx);
        for (int i = tid; i < C4; i += blockDim.x) {
            float4 v = __ldg(row_x4 + i);
            float4 r;
            int idx = i * 4;
            r.x = __expf(v.x - row_max) * inv_sum;
            r.y = __expf(v.y - row_max) * inv_sum;
            r.z = __expf(v.z - row_max) * inv_sum;
            r.w = __expf(v.w - row_max) * inv_sum;
            if (idx + 0 == tgt) r.x -= 1.0f;
            if (idx + 1 == tgt) r.y -= 1.0f;
            if (idx + 2 == tgt) r.z -= 1.0f;
            if (idx + 3 == tgt) r.w -= 1.0f;
            r.x *= scale; r.y *= scale; r.z *= scale; r.w *= scale;
            row_dx4[i] = r;
        }
    } else {
        for (int i = tid; i < C; i += blockDim.x) {
            float ex = __expf((float)__ldg(row_x + i) - row_max);
            float g = ex * inv_sum;
            if (i == tgt) g -= 1.0f;
            g *= scale;
            row_dx[i] = (scalar_t)g;
        }
    }
}

// ---- Host wrapper ----
torch::Tensor backward_cuda(torch::Tensor grad_output,
                            torch::Tensor predictions,
                            torch::Tensor targets)
{
    TORCH_CHECK(grad_output.dim() == 0, "grad_output must be a scalar");
    TORCH_CHECK(predictions.is_cuda() && targets.is_cuda() && grad_output.is_cuda(),
                "All inputs must be CUDA tensors.");

    const int N = predictions.size(0);
    const int C = predictions.size(1);
    auto grad_predictions = torch::empty_like(predictions);
    float grad_val = grad_output.item<float>();

    if (C <= 32) {
        int warps_per_block = 8;
        int threads_per_block = warps_per_block * 32;
        dim3 grid((N + warps_per_block - 1) / warps_per_block);
        AT_DISPATCH_FLOATING_TYPES(predictions.scalar_type(), "ce_backward_rowwise_fvec_kernel", ([&] {
            ce_backward_rowwise_fvec_kernel<scalar_t>
                <<<grid, threads_per_block>>>(
                    static_cast<scalar_t>(grad_val),
                    predictions.data_ptr<scalar_t>(),
                    targets.data_ptr<int64_t>(),
                    grad_predictions.data_ptr<scalar_t>(),
                    N, C, warps_per_block);
        }));
    } else {
        int block_size = 128;
        dim3 grid(N);
        AT_DISPATCH_FLOATING_TYPES(predictions.scalar_type(), "ce_backward_fusedvec_kernel", ([&] {
            ce_backward_fusedvec_kernel<scalar_t>
                <<<grid, block_size>>>(
                    static_cast<scalar_t>(grad_val),
                    predictions.data_ptr<scalar_t>(),
                    targets.data_ptr<int64_t>(),
                    grad_predictions.data_ptr<scalar_t>(),
                    N, C);
        }));
    }

    cudaError_t err = cudaGetLastError();
    if (err != cudaSuccess)
        throw std::runtime_error(cudaGetErrorString(err));
    return grad_predictions;
}

PYBIND11_MODULE(TORCH_EXTENSION_NAME, m) {
    m.def("backward", &backward_cuda, "Cross-Entropy Backward (fused, vec, CUDA)");
}