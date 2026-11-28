#include <torch/extension.h>
#include <cuda.h>
#include <cuda_runtime.h>
#include <vector>
#include <cuda_fp16.h>

// Use 32 for warp size which is standard for CUDA
#define WARP_SIZE 32
// H100 has higher maximum threads per block
#define MAX_THREADS_PER_BLOCK 1024
// Maximum grid size in x dimension
#define MAX_GRID_X 65535

// Helper functions for warp-level reductions
__inline__ __device__ float warpReduceSum(float val) {
    #pragma unroll
    for (int offset = WARP_SIZE/2; offset > 0; offset /= 2) {
        val += __shfl_down_sync(0xffffffff, val, offset);
    }
    return val;
}

// Vector load/store helpers for 4-element vectors when aligned properly
template <typename T>
__device__ __forceinline__ void load_vec4(const T* addr, T& v0, T& v1, T& v2, T& v3) {
    if constexpr (std::is_same<T, float>::value) {
        float4 tmp = *reinterpret_cast<const float4*>(addr);
        v0 = tmp.x;
        v1 = tmp.y;
        v2 = tmp.z;
        v3 = tmp.w;
    } else {
        v0 = addr[0];
        v1 = addr[1];
        v2 = addr[2];
        v3 = addr[3];
    }
}

template <typename T>
__device__ __forceinline__ void store_vec4(T* addr, T v0, T v1, T v2, T v3) {
    if constexpr (std::is_same<T, float>::value) {
        float4 tmp;
        tmp.x = v0;
        tmp.y = v1;
        tmp.z = v2;
        tmp.w = v3;
        *reinterpret_cast<float4*>(addr) = tmp;
    } else {
        addr[0] = v0;
        addr[1] = v1;
        addr[2] = v2;
        addr[3] = v3;
    }
}

template <int BLOCK_SIZE>
__global__ void layernorm_2d_grid_kernel(
    const float* __restrict__ x,
    const float* __restrict__ weight,
    const float* __restrict__ bias,
    float* __restrict__ y,
    float eps,
    int outer_dim,
    int inner_dim) 
{
    // 2D grid for handling large problem sizes
    const int instance = blockIdx.y * gridDim.x + blockIdx.x;
    if (instance >= outer_dim) return;
    
    const int tid = threadIdx.x;
    const int lane_id = tid % WARP_SIZE;
    const int warp_id = tid / WARP_SIZE;
    const int warps_per_block = BLOCK_SIZE / WARP_SIZE;
    
    // Starting offset for this instance
    const int offset = instance * inner_dim;
    
    // Thread local accumulators for sum and sum of squares
    float sum = 0.0f;
    float sum_sq = 0.0f;
    
    // Check if we can use vectorized loads (4x float)
    bool use_vector = (inner_dim % 4 == 0) && ((reinterpret_cast<uintptr_t>(x + offset) % 16) == 0);

    // Step 1: Calculate partial sums and sums of squares with vectorized loads
    if (use_vector) {
        // Process 4 elements at a time using vectorized loads
        for (int i = tid * 4; i < inner_dim; i += BLOCK_SIZE * 4) {
            if (i + 3 < inner_dim) {
                float v0, v1, v2, v3;
                load_vec4(x + offset + i, v0, v1, v2, v3);
                
                sum += v0 + v1 + v2 + v3;
                sum_sq += v0*v0 + v1*v1 + v2*v2 + v3*v3;
            }
        }
    } else {
        // Regular processing for non-aligned or smaller dimensions
        for (int i = tid; i < inner_dim; i += BLOCK_SIZE) {
            float val = x[offset + i];
            sum += val;
            sum_sq += val * val;
        }
    }
    
    // Step 2: Warp-level reduction for sum and sum_sq
    sum = warpReduceSum(sum);
    sum_sq = warpReduceSum(sum_sq);
    
    // Step 3: Inter-warp reduction using shared memory
    __shared__ float warp_sums[32]; // Maximum 32 warps per block on H100
    __shared__ float warp_sum_squares[32];
    
    if (lane_id == 0) {
        warp_sums[warp_id] = sum;
        warp_sum_squares[warp_id] = sum_sq;
    }
    
    __syncthreads();
    
    // First warp reduces all partial sums
    if (warp_id == 0 && lane_id < warps_per_block) {
        sum = warp_sums[lane_id];
        sum_sq = warp_sum_squares[lane_id];
        
        // Final warp-level reduction
        if (warps_per_block > 1) {
            sum = warpReduceSum(sum);
            sum_sq = warpReduceSum(sum_sq);
        }
    }
    
    // Broadcast from the first thread to shared memory
    __shared__ float mean_shared, rstd_shared;
    if (tid == 0) {
        mean_shared = sum / inner_dim;
        float var = sum_sq / inner_dim - mean_shared * mean_shared;
        rstd_shared = rsqrtf(var + eps);
    }
    
    __syncthreads();
    
    // Load the mean and rstd from shared memory
    float mean = mean_shared;
    float rstd = rstd_shared;
    
    // Step 4: Normalize each element and apply affine transformation with coalesced memory access
    if (use_vector) {
        for (int i = tid * 4; i < inner_dim; i += BLOCK_SIZE * 4) {
            if (i + 3 < inner_dim) {
                // Load 4 elements at once
                float x0, x1, x2, x3;
                float w0, w1, w2, w3;
                float b0, b1, b2, b3;
                
                // Load input vector
                load_vec4(x + offset + i, x0, x1, x2, x3);
                // Load weight and bias vectors
                load_vec4(weight + i, w0, w1, w2, w3);
                load_vec4(bias + i, b0, b1, b2, b3);
                
                // Normalize and apply affine transformation
                x0 = ((x0 - mean) * rstd * w0) + b0;
                x1 = ((x1 - mean) * rstd * w1) + b1;
                x2 = ((x2 - mean) * rstd * w2) + b2;
                x3 = ((x3 - mean) * rstd * w3) + b3;
                
                // Store 4 results at once
                store_vec4(y + offset + i, x0, x1, x2, x3);
            }
        }
    } else {
        // Regular processing with improved memory access patterns
        for (int i = tid; i < inner_dim; i += BLOCK_SIZE) {
            float val = x[offset + i];
            float norm_val = (val - mean) * rstd;
            y[offset + i] = norm_val * weight[i] + bias[i];
        }
    }
}

// Template specialization launcher
template <int BLOCK_SIZE>
void launch_layernorm_kernel(
    const torch::Tensor& x,
    const torch::Tensor& weight,
    const torch::Tensor& bias,
    torch::Tensor& y,
    float eps,
    int outer_dim,
    int inner_dim) 
{
    // Use 2D grid for large outer dimensions
    dim3 grid;
    if (outer_dim <= MAX_GRID_X) {
        grid = dim3(outer_dim, 1, 1);
    } else {
        // Calculate grid dimensions for 2D distribution
        int grid_x = MAX_GRID_X;
        int grid_y = (outer_dim + grid_x - 1) / grid_x;
        grid = dim3(grid_x, grid_y, 1);
    }
    
    layernorm_2d_grid_kernel<BLOCK_SIZE><<<grid, BLOCK_SIZE>>>(
        x.data_ptr<float>(),
        weight.data_ptr<float>(),
        bias.data_ptr<float>(),
        y.data_ptr<float>(),
        eps,
        outer_dim,
        inner_dim
    );
}

torch::Tensor layernorm_2d_grid_optimized(torch::Tensor x, torch::Tensor weight, torch::Tensor bias, float eps) {
    // Ensure contiguous memory layout
    x = x.contiguous();
    weight = weight.contiguous();
    bias = bias.contiguous();
    
    int inner_dim = weight.numel();
    int outer_dim = x.numel() / inner_dim;
    
    auto y = torch::empty_like(x);
    
    // Select block size based on inner dimension size for optimal performance
    // These thresholds are based on H100 characteristics
    if (inner_dim <= 128) {
        launch_layernorm_kernel<128>(x, weight, bias, y, eps, outer_dim, inner_dim);
    } else if (inner_dim <= 256) {
        launch_layernorm_kernel<256>(x, weight, bias, y, eps, outer_dim, inner_dim);
    } else if (inner_dim <= 512) {
        launch_layernorm_kernel<512>(x, weight, bias, y, eps, outer_dim, inner_dim);
    } else {
        launch_layernorm_kernel<1024>(x, weight, bias, y, eps, outer_dim, inner_dim);
    }
    
    // Check for launch errors
    cudaError_t err = cudaGetLastError();
    if (err != cudaSuccess) {
        printf("Error in layernorm_2d_grid_kernel: %s\n", cudaGetErrorString(err));
    }
    
    return y;
}

PYBIND11_MODULE(TORCH_EXTENSION_NAME, m) {
    m.def("forward", &layernorm_2d_grid_optimized, "LayerNorm forward with 2D grid optimization (CUDA)");
}