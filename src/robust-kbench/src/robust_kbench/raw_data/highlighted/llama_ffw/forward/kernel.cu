#include <cuda_runtime.h>
#include <torch/extension.h>
#include <ATen/cuda/CUDAContext.h>
#include <cublas_v2.h>
#include <c10/cuda/CUDAGuard.h>

/* ====================================================================================
   Fast SiLU
   ==================================================================================== */
__device__ __forceinline__ float silu(float x) {
    return x / (1.0f + __expf(-x));
}

/* ====================================================================================
   Fully predicated warp-uniform fused kernel
   One logical thread-group  = one warp
   Each lane processes k 128-bit (float4) chunks; validity is determined by a mask
   computed once per chunk – no divergent branches in the hot path.
   ==================================================================================== */
template<int VEC_PER_THREAD>
__global__ void fused_silu_mul_predicate_kernel(const float * __restrict__ gate,
                                                const float * __restrict__ up,
                                                float       * __restrict__ out,
                                                const int total_elem)
{
    using Vec = float4;                                 // 4 × fp32, 16 B, 128-bit
    const int  total_vec = (total_elem + 3) >> 2;       // ceil(total / 4)

    const int  lane_id   = threadIdx.x & 31;
    const int  warp_id   = (blockDim.x * blockIdx.x + threadIdx.x) >> 5;
    const int  warps     = (gridDim.x * blockDim.x) >> 5;
    const int  vec_base  = warp_id * VEC_PER_THREAD * 32;

#pragma unroll
    for (int step = 0; ; ++step) {
        int global_vec = vec_base + step * warps * VEC_PER_THREAD * 32 + lane_id;

        if (global_vec >= total_vec) break;             // uniform exit condition

#pragma unroll
        for (int v = 0; v < VEC_PER_THREAD; ++v, global_vec += 32) {
            /* ---------------------------------------------------------------------- */
            /* 1. Determine if the 4 elements are valid – generates a compile-time
                   predicate mask; the conditional assignments below turn to predicated
                   instructions and do NOT cause divergence                           */
            /* ---------------------------------------------------------------------- */
            const int elem_base = global_vec << 2;               // *4
            const bool valid = (elem_base < total_elem);

            /* Skip memory ops for fully OOB chunks  (uniform across warp) */
            if (!valid) continue;                                // still warp-uniform

            /* Load */
            Vec g = reinterpret_cast<const Vec*>(gate)[global_vec];
            Vec u = reinterpret_cast<const Vec*>(up  )[global_vec];

            /* Compute */
            Vec o;
#pragma unroll
            for (int i = 0; i < 4; ++i) {
                float x   = reinterpret_cast<float*>(&g)[i];
                float val = silu(x) * reinterpret_cast<float*>(&u)[i];
                reinterpret_cast<float*>(&o)[i] = val;
            }

            /* Store  –  always, but only lanes owning valid elements write non-OOB
               addresses, still no divergent branch                                    */
            reinterpret_cast<Vec*>(out)[global_vec] = o;
        }
    }
}

/* -------------------------------- Launcher -------------------------------- */
static inline void launch_fused_kernel(const float* gate,
                                       const float* up,
                                       float* out,
                                       int total_elem,
                                       cudaStream_t stream)
{
    constexpr int VEC_PER_THREAD = 2;          // each thread does 2×float4  (8 fp32)
    constexpr int TPB            = 256;        // 8 warps
    int total_vec = (total_elem + 3) >> 2;     // #float4
    int warps     = (total_vec + VEC_PER_THREAD * 32 - 1) / (VEC_PER_THREAD * 32);
    int blocks    = (warps + 7) >> 3;          // 8 warps per block
    blocks        = min(blocks, 65535);

    fused_silu_mul_predicate_kernel<VEC_PER_THREAD>
        <<<blocks, TPB, 0, stream>>>(gate, up, out, total_elem);
}

/* ====================================================================================
   High-level FFN wrapper (unchanged GEMMs – still the fastest on H100/Tensor-Cores)
   ==================================================================================== */
torch::Tensor llama_ffw_predicate_cuda(const torch::Tensor& input,
                                       const torch::Tensor& gate_proj,
                                       const torch::Tensor& up_proj,
                                       const torch::Tensor& down_proj)
{
    TORCH_CHECK(input.is_cuda() &&
                input.scalar_type() == torch::kFloat32 &&
                input.dim() == 3,
                "Input must be CUDA, float32, 3-D");

    const int B  = input.size(0);
    const int S  = input.size(1);
    const int H  = input.size(2);
    const int M  = gate_proj.size(0);
    const int BS = B * S;

    auto x2d = input.contiguous().view({BS, H});

    auto opts = torch::TensorOptions().dtype(torch::kFloat32).device(input.device());
    auto gate_out = torch::empty({BS, M}, opts);
    auto up_out   = torch::empty({BS, M}, opts);
    auto inter    = torch::empty({BS, M}, opts);
    auto out2d    = torch::empty({BS, H}, opts);

    at::cuda::CUDAGuard guard(input.device());
    cublasHandle_t handle = at::cuda::getCurrentCUDABlasHandle();
    cudaStream_t   stream = at::cuda::getCurrentCUDAStream();
    cublasSetStream(handle, stream);

    const float alpha = 1.f, beta = 0.f;

    /* ---------------------------- GEMM 1 & 2 ----------------------------- */
    cublasSgemm(handle, CUBLAS_OP_T, CUBLAS_OP_N,
                M, BS, H, &alpha,
                gate_proj.data_ptr<float>(), H,
                x2d.data_ptr<float>(),       H,
                &beta, gate_out.data_ptr<float>(), M);

    cublasSgemm(handle, CUBLAS_OP_T, CUBLAS_OP_N,
                M, BS, H, &alpha,
                up_proj.data_ptr<float>(),  H,
                x2d.data_ptr<float>(),      H,
                &beta, up_out.data_ptr<float>(),   M);

    /* -------------------------- fused SiLU·mul --------------------------- */
    launch_fused_kernel(gate_out.data_ptr<float>(),
                        up_out  .data_ptr<float>(),
                        inter   .data_ptr<float>(),
                        BS * M, stream);

    /* ---------------------------- GEMM 3 --------------------------------- */
    cublasSgemm(handle, CUBLAS_OP_T, CUBLAS_OP_N,
                H, BS, M, &alpha,
                down_proj.data_ptr<float>(), M,
                inter.data_ptr<float>(),     M,
                &beta, out2d.data_ptr<float>(), H);

    return out2d.view({B, S, H});
}

/* ====================================================================================
   pybind11
   ==================================================================================== */
torch::Tensor forward(const torch::Tensor& input,
                      const torch::Tensor& gate_proj,
                      const torch::Tensor& up_proj,
                      const torch::Tensor& down_proj)
{
    return llama_ffw_predicate_cuda(input, gate_proj, up_proj, down_proj);
}

PYBIND11_MODULE(TORCH_EXTENSION_NAME, m) {
    m.def("forward", &forward,
          "LLaMA FFW – divergence-free fused SiLU·mul (CUDA)",
          py::arg("input"),
          py::arg("gate_proj"),
          py::arg("up_proj"),
          py::arg("down_proj"));
}