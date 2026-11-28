#include <torch/extension.h>
#include <ATen/cuda/CUDAContext.h>
#include <cuda.h>
#include <cuda_runtime.h>

#define WARP_SIZE 32

template<typename T>
__device__ __forceinline__ T warp_sum(T v) {
    #pragma unroll
    for (int d = WARP_SIZE / 2; d > 0; d >>= 1)
        v += __shfl_down_sync(0xffffffff, v, d);
    return v;
}

template<int BLOCK_SIZE>
__global__ void strided_rms_norm_kernel(
    const float* __restrict__ x,
    const float* __restrict__ w,
    float eps,
    float* __restrict__ y,
    int M,
    int D
) {
    extern __shared__ float s_partials[];
    int row = blockIdx.x;
    if (row >= M) return;
    const float* row_x = x + row * D;
    float* row_y = y + row * D;

    // 1. Compute sum of squares using a strided loop
    float sum = 0.f;
    int tid = threadIdx.x;
    int nthreads = blockDim.x;

    // Vectorized float4 loads where possible
    int D4 = D / 4 * 4;
    for (int i = tid * 4; i < D4; i += nthreads * 4) {
        float4 v4 = reinterpret_cast<const float4*>(row_x)[i / 4];
        sum += v4.x * v4.x + v4.y * v4.y + v4.z * v4.z + v4.w * v4.w;
    }
    for (int i = D4 + tid; i < D; i += nthreads) {
        float v = row_x[i];
        sum += v * v;
    }

    // Block-wide reduction
    sum = warp_sum(sum);
    if ((tid & (WARP_SIZE - 1)) == 0)
        s_partials[tid / WARP_SIZE] = sum;
    __syncthreads();

    float total = 0.f;
    if (tid < nthreads / WARP_SIZE)
        total = s_partials[tid];
    if (tid < WARP_SIZE)
        total = warp_sum(total);
    float norm_factor = 0.f;
    if (tid == 0)
        s_partials[0] = rsqrtf(total / D + eps);
    __syncthreads();
    norm_factor = s_partials[0];

    // 2. Normalize and write out using strided loop (float4 where possible)
    for (int i = tid * 4; i < D4; i += nthreads * 4) {
        float4 v4 = reinterpret_cast<const float4*>(row_x)[i / 4];
        float4 w4 = reinterpret_cast<const float4*>(w)[i / 4];
        float4 y4;
        y4.x = v4.x * norm_factor * w4.x;
        y4.y = v4.y * norm_factor * w4.y;
        y4.z = v4.z * norm_factor * w4.z;
        y4.w = v4.w * norm_factor * w4.w;
        reinterpret_cast<float4*>(row_y)[i / 4] = y4;
    }
    for (int i = D4 + tid; i < D; i += nthreads) {
        row_y[i] = row_x[i] * norm_factor * w[i];
    }
}

static inline int pick_block_size(int D) {
    if (D >= 4096) return 512;
    if (D >= 2048) return 256;
    if (D >= 1024) return 128;
    if (D >= 512)  return 64;
    return 32;
}

template<int BS>
void launch_strided(const float* x, const float* w, float eps, float* y,
                    int M, int D, cudaStream_t s)
{
    size_t smem = (BS / WARP_SIZE) * sizeof(float);
    strided_rms_norm_kernel<BS><<<M, BS, smem, s>>>(x, w, eps, y, M, D);
}

at::Tensor forward(at::Tensor x, at::Tensor w, double eps) {
    TORCH_CHECK(x.is_cuda() && w.is_cuda(), "tensors must be CUDA");
    TORCH_CHECK(x.is_contiguous() && w.is_contiguous(), "tensors must be contiguous");
    TORCH_CHECK(x.scalar_type() == at::kFloat, "only float32 supported");

    int D = x.size(-1);
    int M = x.numel() / D;
    TORCH_CHECK(w.size(0) == D, "weight size mismatch");

    at::Tensor y = at::empty_like(x);
    const float* x_ptr = x.data_ptr<float>();
    const float* w_ptr = w.data_ptr<float>();
    float* y_ptr = y.data_ptr<float>();

    int block = pick_block_size(D);
    cudaStream_t stream = at::cuda::getCurrentCUDAStream();

    switch (block) {
        case 512: launch_strided<512>(x_ptr, w_ptr, static_cast<float>(eps), y_ptr, M, D, stream); break;
        case 256: launch_strided<256>(x_ptr, w_ptr, static_cast<float>(eps), y_ptr, M, D, stream); break;
        case 128: launch_strided<128>(x_ptr, w_ptr, static_cast<float>(eps), y_ptr, M, D, stream); break;
        case 64:  launch_strided<64 >(x_ptr, w_ptr, static_cast<float>(eps), y_ptr, M, D, stream); break;
        default:  launch_strided<32 >(x_ptr, w_ptr, static_cast<float>(eps), y_ptr, M, D, stream); break;
    }

    cudaError_t err = cudaGetLastError();
    TORCH_CHECK(err == cudaSuccess, cudaGetErrorString(err));
    return y;
}

PYBIND11_MODULE(TORCH_EXTENSION_NAME, m) {
    m.def("forward", &forward, "RMSNorm with strided loops for large workloads (CUDA)");
}