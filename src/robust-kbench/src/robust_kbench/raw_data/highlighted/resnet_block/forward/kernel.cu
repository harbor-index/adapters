#include <torch/extension.h>
#include <ATen/cuda/CUDAContext.h>
#include <cuda_runtime.h>

#ifndef MAX_C
#define MAX_C 4096            // 16 KiB for scale+shift combined
#endif

__constant__ float c_scale[MAX_C];
__constant__ float c_shift[MAX_C];

constexpr int NT = 256;

// -----------------------------------------------------------------------------
// Scalar implementation (any dtype)
// -----------------------------------------------------------------------------
template <typename scalar_t, bool WITH_RES, bool WITH_RELU>
__global__ void affine_scalar_kernel(
        const scalar_t *__restrict__ in,
        scalar_t       *__restrict__ out,
        const scalar_t *__restrict__ res,
        int N, int C, int H, int W)
{
    int n       = blockIdx.x / C;
    int c       = blockIdx.x % C;
    int spatial = H * W;
    int base    = (n * C + c) * spatial;

    float s = c_scale[c];
    float b = c_shift[c];

    for (int idx = threadIdx.x; idx < spatial; idx += NT) {
        int off = base + idx;
        float v = static_cast<float>(in[off]) * s + b;
        if constexpr (WITH_RES)  v += static_cast<float>(res[off]);
        if constexpr (WITH_RELU) v = fmaxf(v, 0.f);
        out[off] = static_cast<scalar_t>(v);
    }
}

// -----------------------------------------------------------------------------
// Vectorised implementation (float only, 4-pixel group)
// -----------------------------------------------------------------------------
template <bool WITH_RES, bool WITH_RELU>
__global__ void affine_vec4_kernel(
        const float *__restrict__ in,
        float       *__restrict__ out,
        const float *__restrict__ res,
        int N, int C, int H, int W)
{
    const int spatial     = H * W;              // divisible by 4 – checked by host
    const int vec_spatial = spatial >> 2;       // groups of 4 fp32
    const int n           = blockIdx.x / C;
    const int c           = blockIdx.x % C;
    const int base_vec    = ((n * C + c) * spatial) >> 2;

    float s = c_scale[c];
    float b = c_shift[c];

    const float4 *in4  = reinterpret_cast<const float4 *>(in);
    const float4 *res4 = WITH_RES ? reinterpret_cast<const float4 *>(res) : nullptr;
    float4       *out4 = reinterpret_cast<float4 *>(out);

    for (int idx = threadIdx.x; idx < vec_spatial; idx += NT) {
        int off = base_vec + idx;
        float4 v4 = in4[off];
        float4 r4;

        if constexpr (WITH_RES) r4 = res4[off];

        // Manual unrolling for the 4 components
        float x0 = v4.x * s + b;
        float x1 = v4.y * s + b;
        float x2 = v4.z * s + b;
        float x3 = v4.w * s + b;

        if constexpr (WITH_RES) {
            x0 += r4.x; x1 += r4.y; x2 += r4.z; x3 += r4.w;
        }
        if constexpr (WITH_RELU) {
            x0 = fmaxf(x0, 0.f); x1 = fmaxf(x1, 0.f);
            x2 = fmaxf(x2, 0.f); x3 = fmaxf(x3, 0.f);
        }

        out4[off] = make_float4(x0, x1, x2, x3);
    }
}

// -----------------------------------------------------------------------------
// Upload affine vectors to constant memory
// -----------------------------------------------------------------------------
static inline void copy_to_const(const at::Tensor &scale,
                                 const at::Tensor &shift)
{
    int C = scale.numel();
    TORCH_CHECK(C <= MAX_C, "Channel count exceeds constant-memory buffer");
    auto stream = at::cuda::getCurrentCUDAStream();
    cudaMemcpyToSymbolAsync(c_scale, scale.data_ptr<float>(),
                            C * sizeof(float), 0, cudaMemcpyDeviceToDevice, stream);
    cudaMemcpyToSymbolAsync(c_shift, shift.data_ptr<float>(),
                            C * sizeof(float), 0, cudaMemcpyDeviceToDevice, stream);
}

// -----------------------------------------------------------------------------
// Launch helper – chooses vec4 or scalar path
// -----------------------------------------------------------------------------
template <bool WITH_RES, bool WITH_RELU>
static void launch_affine(at::Tensor       &out,
                          const at::Tensor &inp,
                          const at::Tensor &scale,
                          const at::Tensor &shift,
                          const at::Tensor &residual = at::Tensor())
{
    copy_to_const(scale, shift);

    const int N = inp.size(0), C = inp.size(1);
    const int H = inp.size(2), W = inp.size(3);
    const int blocks = N * C;
    auto stream = at::cuda::getCurrentCUDAStream();

    bool use_vec4 = (inp.scalar_type() == at::kFloat) &&
                    ((H * W) % 4 == 0) &&
                    ((reinterpret_cast<uintptr_t>(inp.data_ptr()) & 0xf) == 0);

    if (use_vec4) {
        affine_vec4_kernel<WITH_RES, WITH_RELU>
           <<<blocks, NT, 0, stream>>>(
                inp.data_ptr<float>(),
                out.data_ptr<float>(),
                WITH_RES ? residual.data_ptr<float>() : nullptr,
                N, C, H, W);
    } else {
        AT_DISPATCH_FLOATING_TYPES(inp.scalar_type(), "affine_scalar_kernel", ([&]{
            affine_scalar_kernel<scalar_t, WITH_RES, WITH_RELU>
               <<<blocks, NT, 0, stream>>>(
                    inp.data_ptr<scalar_t>(),
                    out.data_ptr<scalar_t>(),
                    WITH_RES ? residual.data_ptr<scalar_t>() : nullptr,
                    N, C, H, W);
        }));
    }
    C10_CUDA_KERNEL_LAUNCH_CHECK();
}

// -----------------------------------------------------------------------------
// Forward pass (same high-level flow as before)
// -----------------------------------------------------------------------------
at::Tensor fused_resnet_block_constmem_vec4_forward(
        const at::Tensor &x,
        const at::Tensor &conv1_w,
        const c10::optional<at::Tensor> &conv1_b,
        const at::Tensor &bn1_w,
        const at::Tensor &bn1_b,
        const at::Tensor &bn1_mean,
        const at::Tensor &bn1_var,
        const at::Tensor &conv2_w,
        const c10::optional<at::Tensor> &conv2_b,
        const at::Tensor &bn2_w,
        const at::Tensor &bn2_b,
        const at::Tensor &bn2_mean,
        const at::Tensor &bn2_var,
        const c10::optional<at::Tensor> &down_w,
        const c10::optional<at::Tensor> &down_b,
        const c10::optional<at::Tensor> &down_bn_w,
        const c10::optional<at::Tensor> &down_bn_b,
        const c10::optional<at::Tensor> &down_bn_mean,
        const c10::optional<at::Tensor> &down_bn_var,
        int64_t stride,
        double eps)
{
    using at::IntArrayRef;

    auto make_aff = [&](const at::Tensor &g,const at::Tensor &b,
                        const at::Tensor &m,const at::Tensor &v){
        at::Tensor sc = g * (v + eps).rsqrt();
        at::Tensor sh = b - m * sc;
        return std::pair<at::Tensor,at::Tensor>{sc.contiguous(), sh.contiguous()};
    };

    // 1) Conv-1
    auto y1 = at::conv2d(x, conv1_w, conv1_b,
                         IntArrayRef{stride,stride},
                         IntArrayRef{1,1},
                         IntArrayRef{1,1}, 1);

    // BN-1 + ReLU
    auto [s1,t1] = make_aff(bn1_w,bn1_b,bn1_mean,bn1_var);
    at::Tensor y1a = at::empty_like(y1);
    launch_affine</*WITH_RES=*/false, /*WITH_RELU=*/true>(y1a, y1, s1, t1);

    // 2) Conv-2
    auto y2 = at::conv2d(y1a, conv2_w, conv2_b,
                         IntArrayRef{1,1},
                         IntArrayRef{1,1},
                         IntArrayRef{1,1}, 1);

    // Residual path
    at::Tensor ident = x;
    if (down_w.has_value()) {
        ident = at::conv2d(x, *down_w, down_b,
                           IntArrayRef{stride,stride},
                           IntArrayRef{0,0},
                           IntArrayRef{1,1}, 1);
        auto [sd,td] = make_aff(*down_bn_w,*down_bn_b,
                                *down_bn_mean,*down_bn_var);
        at::Tensor tmp = at::empty_like(ident);
        launch_affine</*WITH_RES=*/false, /*WITH_RELU=*/false>(tmp, ident, sd, td);
        ident = tmp;
    }

    // BN-2 + Add + ReLU
    auto [s2,t2] = make_aff(bn2_w,bn2_b,bn2_mean,bn2_var);
    at::Tensor out = at::empty_like(y2);
    launch_affine</*WITH_RES=*/true, /*WITH_RELU=*/true>(out, y2, s2, t2, ident);

    return out;
}

// -----------------------------------------------------------------------------
// Pybind
// -----------------------------------------------------------------------------
PYBIND11_MODULE(TORCH_EXTENSION_NAME, m) {
    m.def("forward", &fused_resnet_block_constmem_vec4_forward,
          "ResNet BasicBlock with const-mem affine & vec4 loads",
          py::arg("x"),
          py::arg("conv1_weight"), py::arg("conv1_bias"),
          py::arg("bn1_weight"),   py::arg("bn1_bias"),
          py::arg("bn1_mean"),     py::arg("bn1_var"),
          py::arg("conv2_weight"), py::arg("conv2_bias"),
          py::arg("bn2_weight"),   py::arg("bn2_bias"),
          py::arg("bn2_mean"),     py::arg("bn2_var"),
          py::arg("downsample_weight")    = c10::nullopt,
          py::arg("downsample_bias")      = c10::nullopt,
          py::arg("downsample_bn_weight") = c10::nullopt,
          py::arg("downsample_bn_bias")   = c10::nullopt,
          py::arg("downsample_bn_mean")   = c10::nullopt,
          py::arg("downsample_bn_var")    = c10::nullopt,
          py::arg("stride") = 1,
          py::arg("eps")    = 1e-5);
}