#include <torch/extension.h>
#include <cuda.h>
#include <cuda_runtime.h>
#include <c10/cuda/CUDAStream.h>

template <typename scalar_t>
__global__ void layer_norm_backward_kernel(
    const scalar_t* __restrict__ grad_output,  // [N, C, H, W]
    const scalar_t* __restrict__ input,        // [N, C, H, W]
    const scalar_t* __restrict__ weight,       // [C, H, W]
    const scalar_t* __restrict__ mean,         // [N]
    const scalar_t* __restrict__ invstd,       // [N]
    scalar_t* __restrict__ grad_input,         // [N, C, H, W]
    scalar_t* __restrict__ grad_weight,        // [C, H, W]
    scalar_t* __restrict__ grad_bias,          // [C, H, W]
    const int N,                               // batch size
    const int C,                               // num features
    const int H,                               // height
    const int W,                               // width
    const float eps)
{
    const int feature_size = C * H * W;
    const int n = blockIdx.y;
    if (n >= N) return;

    // Shared memory layout
    extern __shared__ char shared[];
    scalar_t* sum_dy = (scalar_t*)shared;                          // [threads]
    scalar_t* sum_dy_xmu = (scalar_t*)&sum_dy[blockDim.x];        // [threads]
    scalar_t* sum_dy_w = (scalar_t*)&sum_dy_xmu[blockDim.x];      // [threads]

    const int thread_id = threadIdx.x;
    const int num_threads = blockDim.x;

    // Load batch statistics (broadcast to all threads in the block)
    const scalar_t batch_mean = mean[n];
    const scalar_t batch_invstd = invstd[n];
    const scalar_t batch_var = 1.0f / (batch_invstd * batch_invstd) - eps;

    // Initialize partial sums
    sum_dy[thread_id] = 0;
    sum_dy_xmu[thread_id] = 0;
    sum_dy_w[thread_id] = 0;

    // First pass: compute sums for gradients
    for (int i = thread_id; i < feature_size; i += num_threads) {
        const int idx = n * feature_size + i;
        const scalar_t x_i = input[idx];
        const scalar_t dy_i = grad_output[idx];
        const scalar_t w_i = weight[i];
        const scalar_t xmu_i = x_i - batch_mean;
        
        // Accumulate partial sums
        sum_dy[thread_id] += dy_i;
        sum_dy_xmu[thread_id] += dy_i * xmu_i;
        sum_dy_w[thread_id] += dy_i * w_i;
    }

    // Reduce partial sums within the block
    __syncthreads();
    for (int stride = num_threads / 2; stride > 0; stride >>= 1) {
        if (thread_id < stride) {
            sum_dy[thread_id] += sum_dy[thread_id + stride];
            sum_dy_xmu[thread_id] += sum_dy_xmu[thread_id + stride];
            sum_dy_w[thread_id] += sum_dy_w[thread_id + stride];
        }
        __syncthreads();
    }

    // Compute mean values
    const scalar_t inv_feature_size = scalar_t(1) / feature_size;
    if (thread_id == 0) {
        sum_dy[0] *= inv_feature_size;
        sum_dy_xmu[0] *= inv_feature_size;
        sum_dy_w[0] *= inv_feature_size;
    }
    __syncthreads();

    const scalar_t mean_dy = sum_dy[0];
    const scalar_t mean_dy_xmu = sum_dy_xmu[0];
    const scalar_t mean_dy_w = sum_dy_w[0];

    // Second pass: compute gradients
    for (int i = thread_id; i < feature_size; i += num_threads) {
        const int idx = n * feature_size + i;
        const scalar_t x_i = input[idx];
        const scalar_t dy_i = grad_output[idx];
        const scalar_t w_i = weight[i];
        const scalar_t xmu_i = x_i - batch_mean;

        // Compute grad_input
        // Split computation for better numerical stability
        const scalar_t common_term = dy_i - mean_dy;
        const scalar_t correction_term = (xmu_i * mean_dy_xmu) / (batch_var + eps);
        grad_input[idx] = batch_invstd * w_i * (common_term - correction_term);

        // Compute grad_weight and grad_bias
        // Use double-precision accumulation for better accuracy
        atomicAdd(&grad_weight[i], scalar_t(double(dy_i) * double(xmu_i) * double(batch_invstd)));
        atomicAdd(&grad_bias[i], dy_i);
    }
}

std::tuple<torch::Tensor, torch::Tensor, torch::Tensor> backward(
    torch::Tensor grad_output,
    torch::Tensor input,
    torch::Tensor weight,
    torch::Tensor bias,
    float eps,
    std::vector<int64_t> normalized_shape)
{
    TORCH_CHECK(input.dim() == 4, "Input must be 4D tensor");
    TORCH_CHECK(grad_output.dim() == 4, "grad_output must be 4D tensor");

    const int N = input.size(0);
    const int C = input.size(1);
    const int H = input.size(2);
    const int W = input.size(3);

    // Ensure inputs are contiguous
    grad_output = grad_output.contiguous();
    input = input.contiguous();
    weight = weight.contiguous();

    // Compute statistics with double precision for better accuracy
    auto options = input.options().dtype(torch::kFloat64);
    auto x_double = input.to(options);
    
    auto normalized_dims = std::vector<int64_t>{1, 2, 3};
    auto mean = x_double.mean(normalized_dims, /*keepdim=*/true);
    auto var = x_double.var(normalized_dims, /*keepdim=*/true, /*unbiased=*/false);
    auto invstd = torch::rsqrt(var + eps);

    // Convert back to original dtype
    mean = mean.to(input.dtype());
    invstd = invstd.to(input.dtype());

    // Allocate output tensors
    auto grad_input = torch::empty_like(input);
    auto grad_weight = torch::zeros_like(weight);
    auto grad_bias = torch::zeros_like(bias);

    // Launch configuration
    const int threads = 256;
    const dim3 blocks(1, N);  // One block per batch item
    const int shared_mem_size = 3 * threads * sizeof(float);

    // Get CUDA stream
    cudaStream_t stream = at::cuda::getCurrentCUDAStream();

    AT_DISPATCH_FLOATING_TYPES(input.scalar_type(), "layer_norm_backward_kernel", ([&] {
        layer_norm_backward_kernel<scalar_t><<<blocks, threads, shared_mem_size, stream>>>(
            grad_output.data_ptr<scalar_t>(),
            input.data_ptr<scalar_t>(),
            weight.data_ptr<scalar_t>(),
            mean.data_ptr<scalar_t>(),
            invstd.data_ptr<scalar_t>(),
            grad_input.data_ptr<scalar_t>(),
            grad_weight.data_ptr<scalar_t>(),
            grad_bias.data_ptr<scalar_t>(),
            N, C, H, W, eps
        );
    }));

    return std::make_tuple(grad_input, grad_weight, grad_bias);
}

PYBIND11_MODULE(TORCH_EXTENSION_NAME, m) {
    m.def("backward", &backward, "LayerNorm backward (CUDA)");
}