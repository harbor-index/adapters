#include <torch/extension.h>
#include <cuda.h>
#include <cuda_runtime.h>
#include <vector>

#define WARP_SIZE 32
#define WARPS_PER_BLOCK 4  // blockDim.x = 128

template<typename scalar_t>
__global__ void linear_relu_shfl_strideloop_kernel(
    const scalar_t* __restrict__ input,    // [B, I]
    const scalar_t* __restrict__ weights,  // [O, I]
    const scalar_t* __restrict__ bias,     // [O]
    scalar_t* __restrict__ output,         // [B, O]
    int B, int I, int O)
{
    int threads_per_block = blockDim.x;
    int blocks_x = gridDim.x;
    int blocks_y = gridDim.y;

    int warps_per_block = threads_per_block / WARP_SIZE;
    int global_warp_id = (blockIdx.y * blocks_x + blockIdx.x) * warps_per_block + threadIdx.x / WARP_SIZE;
    int total_warps = blocks_x * blocks_y * warps_per_block;

    int lane_id = threadIdx.x & (WARP_SIZE - 1);

    // Stride loop over (batch, out_feature) pairs, each warp computes one output per loop
    for (int idx = global_warp_id; idx < B * O; idx += total_warps) {
        int batch_idx = idx / O;
        int out_idx   = idx % O;

        if (batch_idx < B && out_idx < O) {
            const scalar_t* x_row = input  + batch_idx * I;
            const scalar_t* w_row = weights + out_idx   * I;

            // Each lane computes partial dot-product
            scalar_t sum = scalar_t(0);
            for (int k = lane_id; k < I; k += WARP_SIZE) {
                sum += x_row[k] * w_row[k];
            }

            // Warp-wide reduction using shuffle
            unsigned full_mask = 0xffffffffu;
            for (int offset = WARP_SIZE / 2; offset > 0; offset >>= 1) {
                sum += __shfl_down_sync(full_mask, sum, offset);
            }

            // Warp leader writes result
            if (lane_id == 0) {
                scalar_t val = sum + bias[out_idx];
                output[batch_idx * O + out_idx] = val > scalar_t(0) ? val : scalar_t(0);
            }
        }
    }
}

torch::Tensor linear_relu_shfl_strideloop_forward(
    torch::Tensor input,    // [B, I]
    torch::Tensor weights,  // [O, I]
    torch::Tensor bias)     // [O]
{
    TORCH_CHECK(input.is_contiguous(),  "input must be contiguous");
    TORCH_CHECK(weights.is_contiguous(),"weights must be contiguous");
    TORCH_CHECK(bias.is_contiguous(),   "bias must be contiguous");
    int B = input.size(0);
    int I = input.size(1);
    int O = weights.size(0);

    auto output = torch::empty({B, O}, input.options());

    // Launch configuration: each block has WARPS_PER_BLOCK warps
    int threads = WARP_SIZE * WARPS_PER_BLOCK;
    // Use a 2D grid for flexibility and occupancy
    int max_blocks = 512; // tune for your device
    int blocks_x = std::min((O + WARPS_PER_BLOCK - 1) / WARPS_PER_BLOCK, max_blocks);
    int blocks_y = std::min((B + 1), max_blocks);

    dim3 grid(blocks_x, blocks_y);
    dim3 block(threads);

    AT_DISPATCH_FLOATING_TYPES(input.scalar_type(), "linear_relu_shfl_strideloop_forward", ([&] {
        linear_relu_shfl_strideloop_kernel<scalar_t><<<grid, block>>>(
            input.data_ptr<scalar_t>(),
            weights.data_ptr<scalar_t>(),
            bias.data_ptr<scalar_t>(),
            output.data_ptr<scalar_t>(),
            B, I, O);
    }));

    cudaError_t err = cudaGetLastError();
    if (err != cudaSuccess) {
        throw std::runtime_error("CUDA error: " + std::string(cudaGetErrorString(err)));
    }
    return output;
}

PYBIND11_MODULE(TORCH_EXTENSION_NAME, m) {
    m.def("forward", &linear_relu_shfl_strideloop_forward, "Linear+ReLU with warp-shuffle and grid-stride loop");
}