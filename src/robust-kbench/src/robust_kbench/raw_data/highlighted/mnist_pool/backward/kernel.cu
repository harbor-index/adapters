#include <torch/extension.h>
#include <pybind11/pybind11.h>
#include <ATen/ATen.h>
#include <ATen/cuda/CUDAContext.h>
#include <cuda.h>
#include <cuda_runtime.h>

namespace py = pybind11;

/* -------------------------- device kernel -------------------------------- */

template <typename scalar_t>
__global__ void maxpool2d_ks2_backward_fast_kernel(
        const scalar_t* __restrict__ x,
        const scalar_t* __restrict__ grad_out,
              scalar_t* __restrict__ grad_in,
        int N, int C, int H, int W,
        int outH, int outW,
        long total_out)               // N*C*outH*outW
{
    // grid-stride loop – lets us choose any grid size
    for (long idx = blockIdx.x * blockDim.x + threadIdx.x;
         idx < total_out;
         idx += gridDim.x * blockDim.x)
    {
        // unravel linear index ->  n, c, h_out, w_out
        int w_out = idx % outW;
        int tmp   = idx / outW;
        int h_out = tmp % outH;
        tmp       = tmp / outH;
        int c     = tmp % C;
        int n     = tmp / C;

        // pointer to the plane (n,c)
        const long plane_offset_X  = ((long)n * C + c) * H * W;
        const long plane_offset_GO = ((long)n * C + c) * outH * outW;

        // top-left of the 2×2 window in input
        int h_in = h_out << 1;    // *2
        int w_in = w_out << 1;

        const scalar_t* x_ptr = x + plane_offset_X + (long)h_in * W + w_in;

        // load the four values (use read-only cache)
        scalar_t v00 = __ldg(x_ptr);           // (0,0)
        scalar_t v01 = __ldg(x_ptr + 1);       // (0,1)
        scalar_t v10 = __ldg(x_ptr + W);       // (1,0)
        scalar_t v11 = __ldg(x_ptr + W + 1);   // (1,1)

        // locate the maximum
        scalar_t maxv = v00;  int offset = 0;
        if (v01 > maxv) { maxv = v01; offset = 1; }
        if (v10 > maxv) { maxv = v10; offset =  W; }
        if (v11 > maxv) { maxv = v11; offset =  W + 1; }

        // route upstream gradient (grad_input is zero-initialised -> 1 write)
        grad_in[plane_offset_X + (long)h_in * W + w_in + offset] =
            grad_out[plane_offset_GO + (long)h_out * outW + w_out];
    }
}

/* --------------------------- launcher ------------------------------------ */

at::Tensor maxpool2d_ks2_backward_fast(
        const at::Tensor& x,
        const at::Tensor& grad_out)
{
    TORCH_CHECK(x.is_cuda() && grad_out.is_cuda(),
                "tensors must be CUDA");

    const int N = x.size(0);
    const int C = x.size(1);
    const int H = x.size(2);
    const int W = x.size(3);
    const int outH = grad_out.size(2);
    const int outW = grad_out.size(3);

    // grad_input initialised to zero once; no extra writes in the kernel
    auto grad_in = at::zeros_like(x);

    const long total = 1L * N * C * outH * outW;

    /* kernel configuration:
       – 256 threads / block gives good occupancy on H100
       – blocks = min( ceil(total/threads),  65535 )             */
    const int threads = 256;
    const int maxBlocks = 65535;
    int blocks = (int)std::min( (total + threads - 1) / threads,
                                (long)maxBlocks );

    cudaStream_t stream = at::cuda::getCurrentCUDAStream();

    AT_DISPATCH_FLOATING_TYPES_AND_HALF(
        x.scalar_type(), "maxpool2d_ks2_backward_fast", [&]
    {
        maxpool2d_ks2_backward_fast_kernel<scalar_t>
            <<<blocks, threads, 0, stream>>>(
                x.data_ptr<scalar_t>(),
                grad_out.data_ptr<scalar_t>(),
                grad_in.data_ptr<scalar_t>(),
                N, C, H, W, outH, outW, total);
    });

    return grad_in;
}

/* --------------------------- python glue --------------------------------- */

at::Tensor backward_wrapper(py::tuple saved, const at::Tensor& grad_out)
{
    TORCH_CHECK(saved.size() == 1,
                "expected a single saved tensor");
    at::Tensor x = saved[0].cast<at::Tensor>();
    return maxpool2d_ks2_backward_fast(x, grad_out);
}

PYBIND11_MODULE(TORCH_EXTENSION_NAME, m)
{
    m.def("backward", &backward_wrapper,
          "2×2-stride-2 max-pool backward (fast, no atomics)");
}