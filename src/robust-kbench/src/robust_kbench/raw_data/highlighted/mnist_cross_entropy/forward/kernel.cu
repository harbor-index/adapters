#include <torch/extension.h>
#include <ATen/cuda/CUDAContext.h>
#include <cuda.h>
#include <cuda_runtime.h>

#define THREADS_PER_BLOCK 256
#define WARP_SIZE 32
#define SAMPLES_PER_BLOCK (THREADS_PER_BLOCK / WARP_SIZE)

// Template kernel for known class count (unrolled)
template<int NUM_CLASSES>
__global__ void cross_entropy_sharedwarp_unroll_kernel(
    const float* __restrict__ preds,
    const int64_t* __restrict__ targets,
    float* __restrict__ loss,
    int num_samples)
{
    extern __shared__ float shmem[];
    int tid      = threadIdx.x;
    int warp_id  = tid / WARP_SIZE;
    int lane     = tid % WARP_SIZE;
    int sample_i = blockIdx.x * SAMPLES_PER_BLOCK + warp_id;

    if (sample_i < num_samples) {
        const float* row_g = preds + sample_i * NUM_CLASSES;
        float* row_s = shmem + warp_id * NUM_CLASSES;

        // 1) Load row into shared memory (unrolled)
        #pragma unroll
        for (int j = lane; j < NUM_CLASSES; j += WARP_SIZE) {
            row_s[j] = row_g[j];
        }
        __syncwarp();

        // 2) Find max (unrolled)
        float local_max = -1e30f;
        #pragma unroll
        for (int j = lane; j < NUM_CLASSES; j += WARP_SIZE) {
            local_max = fmaxf(local_max, row_s[j]);
        }
        #pragma unroll
        for (int offset = WARP_SIZE/2; offset > 0; offset >>= 1) {
            float m = __shfl_down_sync(0xffffffff, local_max, offset);
            local_max = fmaxf(local_max, m);
        }
        float max_val = __shfl_sync(0xffffffff, local_max, 0);

        // 3) Sum exp (unrolled)
        float local_sum = 0.0f;
        #pragma unroll
        for (int j = lane; j < NUM_CLASSES; j += WARP_SIZE) {
            local_sum += __expf(row_s[j] - max_val);
        }
        #pragma unroll
        for (int offset = WARP_SIZE/2; offset > 0; offset >>= 1) {
            float s = __shfl_down_sync(0xffffffff, local_sum, offset);
            local_sum += s;
        }
        float sum_exp = __shfl_sync(0xffffffff, local_sum, 0);

        int64_t t = targets[sample_i];
        float tval = row_s[t];

        float logp = tval - max_val - __logf(sum_exp);
        if (lane == 0) {
            atomicAdd(loss, -logp);
        }
    }
}

// Fallback kernel for unknown/large class count (partial unrolling)
__global__ void cross_entropy_sharedwarp_unroll_kernel_dynamic(
    const float* __restrict__ preds,
    const int64_t* __restrict__ targets,
    float* __restrict__ loss,
    int num_samples,
    int num_classes)
{
    extern __shared__ float shmem[];
    int tid      = threadIdx.x;
    int warp_id  = tid / WARP_SIZE;
    int lane     = tid % WARP_SIZE;
    int sample_i = blockIdx.x * SAMPLES_PER_BLOCK + warp_id;

    if (sample_i < num_samples) {
        const float* row_g = preds + sample_i * num_classes;
        float* row_s = shmem + warp_id * num_classes;

        // 1) Load row into shared memory
        #pragma unroll 8
        for (int j = lane; j < num_classes; j += WARP_SIZE) {
            row_s[j] = row_g[j];
        }
        __syncwarp();

        // 2) Find max
        float local_max = -1e30f;
        #pragma unroll 8
        for (int j = lane; j < num_classes; j += WARP_SIZE) {
            local_max = fmaxf(local_max, row_s[j]);
        }
        #pragma unroll
        for (int offset = WARP_SIZE/2; offset > 0; offset >>= 1) {
            float m = __shfl_down_sync(0xffffffff, local_max, offset);
            local_max = fmaxf(local_max, m);
        }
        float max_val = __shfl_sync(0xffffffff, local_max, 0);

        // 3) Sum exp
        float local_sum = 0.0f;
        #pragma unroll 8
        for (int j = lane; j < num_classes; j += WARP_SIZE) {
            local_sum += __expf(row_s[j] - max_val);
        }
        #pragma unroll
        for (int offset = WARP_SIZE/2; offset > 0; offset >>= 1) {
            float s = __shfl_down_sync(0xffffffff, local_sum, offset);
            local_sum += s;
        }
        float sum_exp = __shfl_sync(0xffffffff, local_sum, 0);

        int64_t t = targets[sample_i];
        float tval = row_s[t];

        float logp = tval - max_val - __logf(sum_exp);
        if (lane == 0) {
            atomicAdd(loss, -logp);
        }
    }
}

// Host wrapper dispatches the correct kernel for common class counts
torch::Tensor forward(torch::Tensor predictions, torch::Tensor targets) {
    TORCH_CHECK(predictions.is_cuda(), "predictions must be CUDA");
    TORCH_CHECK(targets.is_cuda(),     "targets must be CUDA");
    TORCH_CHECK(predictions.dim()==2,   "predictions must be 2D");
    TORCH_CHECK(targets.dim()==1,       "targets must be 1D");
    TORCH_CHECK(predictions.size(0)==targets.size(0),
                "batch size mismatch");

    int N = predictions.size(0);
    int C = predictions.size(1);
    int threads = THREADS_PER_BLOCK;
    int blocks  = (N + SAMPLES_PER_BLOCK - 1) / SAMPLES_PER_BLOCK;

    auto loss_tensor = torch::zeros({1}, predictions.options());
    float* d_loss = loss_tensor.data_ptr<float>();

    const float*  d_preds  = predictions.data_ptr<float>();
    const int64_t* d_tgts  = targets.data_ptr<int64_t>();

    size_t shmem_bytes = size_t(SAMPLES_PER_BLOCK) * C * sizeof(float);

    // Specialize for common MNIST case (C==10), otherwise fallback
    if (C == 10) {
        cross_entropy_sharedwarp_unroll_kernel<10>
            <<<blocks, threads, shmem_bytes, at::cuda::getCurrentCUDAStream()>>>(
                d_preds, d_tgts, d_loss, N);
    } else if (C == 100) {
        cross_entropy_sharedwarp_unroll_kernel<100>
            <<<blocks, threads, shmem_bytes, at::cuda::getCurrentCUDAStream()>>>(
                d_preds, d_tgts, d_loss, N);
    } else {
        cross_entropy_sharedwarp_unroll_kernel_dynamic
            <<<blocks, threads, shmem_bytes, at::cuda::getCurrentCUDAStream()>>>(
                d_preds, d_tgts, d_loss, N, C);
    }
    return loss_tensor / static_cast<float>(N);
}

PYBIND11_MODULE(TORCH_EXTENSION_NAME, m) {
    m.def("forward", &forward, "CrossEntropy (shared-mem warp, unrolled) forward (CUDA)");
}