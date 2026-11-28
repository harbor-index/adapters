#include <torch/extension.h>
#include <ATen/cuda/CUDAContext.h>
#include <cuda.h>
#include <cuda_runtime.h>
#include <c10/cuda/CUDAMathCompat.h>

// Block size settings (can be tuned)
#define BLOCK_GRAD_INPUT 128
#define BLOCKX_WEIGHT 64
#define BLOCKY_WEIGHT 4
#define BLOCK_BIAS 256

// 1D row-parallel kernel for grad_input: out = grad_output @ weights^T
template <typename scalar_t>
__global__ void grad_input_adaptive_kernel(
    const scalar_t* __restrict__ grad_output,  // (B, O)
    const scalar_t* __restrict__ weights,      // (O, I)
    scalar_t* __restrict__ grad_input,         // (B, I)
    int B, int I, int O) {

    int row = blockIdx.y;
    int tx = threadIdx.x;
    for (int col = blockIdx.x * BLOCK_GRAD_INPUT + tx; col < I; col += blockDim.x * gridDim.x) {
        if (row >= B || col >= I) return;
        scalar_t sum = 0;
        // Unroll by 4
        int k = 0;
        #pragma unroll
        for (; k + 3 < O; k += 4)
            sum += grad_output[row * O + k    ] * weights[k    * I + col] +
                   grad_output[row * O + k + 1] * weights[(k + 1) * I + col] +
                   grad_output[row * O + k + 2] * weights[(k + 2) * I + col] +
                   grad_output[row * O + k + 3] * weights[(k + 3) * I + col];
        for (; k < O; ++k)
            sum += grad_output[row * O + k] * weights[k * I + col];
        grad_input[row * I + col] = sum;
    }
}

// 2D block kernel for grad_weights: out = grad_output^T @ x
template <typename scalar_t>
__global__ void grad_weights_adaptive_kernel(
    const scalar_t* __restrict__ grad_output,  // (B, O)
    const scalar_t* __restrict__ x,            // (B, I)
    scalar_t* __restrict__ grad_weights,       // (O, I)
    int B, int O, int I) {

    int o = blockIdx.y * BLOCKY_WEIGHT + threadIdx.y;
    int i = blockIdx.x * BLOCKX_WEIGHT + threadIdx.x;
    if (o >= O || i >= I) return;
    scalar_t sum = 0;
    int b = 0;
    #pragma unroll
    for (; b + 3 < B; b += 4)
        sum += grad_output[(b    ) * O + o] * x[(b    ) * I + i] +
               grad_output[(b + 1) * O + o] * x[(b + 1) * I + i] +
               grad_output[(b + 2) * O + o] * x[(b + 2) * I + i] +
               grad_output[(b + 3) * O + o] * x[(b + 3) * I + i];
    for (; b < B; ++b)
        sum += grad_output[b * O + o] * x[b * I + i];
    grad_weights[o * I + i] = sum;
}

// bias reduction kernel: block per output feature, 256 threads per block
template <typename scalar_t>
__global__ void grad_bias_adaptive_kernel(
    const scalar_t* __restrict__ grad_output,  // (B, O)
    scalar_t* __restrict__ grad_bias,          // (O)
    int B, int O) {

    int o = blockIdx.x;
    __shared__ scalar_t sdata[BLOCK_BIAS];
    scalar_t sum = 0;
    for (int b = threadIdx.x; b < B; b += blockDim.x)
        sum += grad_output[b * O + o];
    sdata[threadIdx.x] = sum;
    __syncthreads();

    // block-wide reduction
    for (int stride = BLOCK_BIAS / 2; stride > 0; stride /= 2) {
        if (threadIdx.x < stride)
            sdata[threadIdx.x] += sdata[threadIdx.x + stride];
        __syncthreads();
    }
    if (threadIdx.x == 0)
        grad_bias[o] = sdata[0];
}

std::vector<at::Tensor> backward(
    at::Tensor grad_output,
    at::Tensor x,
    at::Tensor weights) {
    int B = grad_output.size(0);
    int O = grad_output.size(1);
    int I = x.size(1);

    auto grad_input = at::empty({B, I}, grad_output.options());
    auto grad_weights = at::empty({O, I}, grad_output.options());
    auto grad_bias = at::empty({O}, grad_output.options());

    // grad_input: block per row, each block processes BLOCK_GRAD_INPUT columns
    dim3 block_input(BLOCK_GRAD_INPUT);
    dim3 grid_input((I + BLOCK_GRAD_INPUT - 1) / BLOCK_GRAD_INPUT, B);

    AT_DISPATCH_FLOATING_TYPES(grad_output.scalar_type(), "grad_input_adaptive_kernel", ([&] {
        grad_input_adaptive_kernel<scalar_t><<<grid_input, block_input, 0, at::cuda::getCurrentCUDAStream()>>>(
            grad_output.data_ptr<scalar_t>(),
            weights.data_ptr<scalar_t>(),
            grad_input.data_ptr<scalar_t>(),
            B, I, O);
    }));

    // grad_weights: 2D grid
    dim3 block_weight(BLOCKX_WEIGHT, BLOCKY_WEIGHT);
    dim3 grid_weight((I + BLOCKX_WEIGHT - 1) / BLOCKX_WEIGHT,
                    (O + BLOCKY_WEIGHT - 1) / BLOCKY_WEIGHT);

    AT_DISPATCH_FLOATING_TYPES(grad_output.scalar_type(), "grad_weights_adaptive_kernel", ([&] {
        grad_weights_adaptive_kernel<scalar_t><<<grid_weight, block_weight, 0, at::cuda::getCurrentCUDAStream()>>>(
            grad_output.data_ptr<scalar_t>(),
            x.data_ptr<scalar_t>(),
            grad_weights.data_ptr<scalar_t>(),
            B, O, I);
    }));

    // grad_bias: block per output feature, 256 threads per block
    dim3 block_bias(BLOCK_BIAS);
    dim3 grid_bias(O);

    AT_DISPATCH_FLOATING_TYPES(grad_output.scalar_type(), "grad_bias_adaptive_kernel", ([&] {
        grad_bias_adaptive_kernel<scalar_t><<<grid_bias, block_bias, 0, at::cuda::getCurrentCUDAStream()>>>(
            grad_output.data_ptr<scalar_t>(),
            grad_bias.data_ptr<scalar_t>(),
            B, O);
    }));

    return {grad_input, grad_weights, grad_bias};
}

PYBIND11_MODULE(TORCH_EXTENSION_NAME, m) {
    m.def("backward", &backward, "Adaptive block size linear backward CUDA kernel");
}