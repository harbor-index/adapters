#include <torch/extension.h>
#include <cuda_runtime.h>

#define WARP_SIZE 32

// Each block (one warp) computes y[row, col].
// Threads in the warp cooperatively load and multiply-add elements,
// then reduce via warp‐shuffle, and thread 0 adds bias and writes output.
__global__ void linear_forward_warp_kernel(
    const float* __restrict__ x,
    const float* __restrict__ weights,
    const float* __restrict__ bias,
    float* __restrict__ y,
    int m,
    int n,
    int k
) {
    int col = blockIdx.x;
    int row = blockIdx.y;
    if (row >= m || col >= n) return;

    int lane = threadIdx.x;  // [0..31]
    float acc = 0.0f;

    // each lane accumulates x[row,k] * weights[col,k] over k in strides of warpSize
    for (int p = lane; p < k; p += WARP_SIZE) {
        acc += x[row * k + p] * weights[col * k + p];
    }

    // warp‐level reduction using shuffle down
    #pragma unroll
    for (int offset = WARP_SIZE/2; offset > 0; offset >>= 1) {
        acc += __shfl_down_sync(0xffffffff, acc, offset);
    }

    // lane 0 writes the final result + bias
    if (lane == 0) {
        y[row * n + col] = acc + bias[col];
    }
}

torch::Tensor forward(torch::Tensor x, torch::Tensor weights, torch::Tensor bias) {
    x = x.contiguous();
    weights = weights.contiguous();
    bias = bias.contiguous();

    int m = x.size(0);
    int k = x.size(1);
    int n = weights.size(0);

    auto y = torch::empty({m, n}, x.options());

    dim3 block(WARP_SIZE);
    dim3 grid(n, m);
    linear_forward_warp_kernel<<<grid, block>>>(
        x.data_ptr<float>(),
        weights.data_ptr<float>(),
        bias.data_ptr<float>(),
        y.data_ptr<float>(),
        m, n, k
    );
    return y;
}

PYBIND11_MODULE(TORCH_EXTENSION_NAME, m) {
    m.def("forward", &forward, "Linear layer forward pass using warp‐shuffle (CUDA)");
}