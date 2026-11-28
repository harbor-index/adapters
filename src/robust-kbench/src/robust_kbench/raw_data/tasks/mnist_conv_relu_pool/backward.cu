#include <torch/extension.h>
#include <vector>          // ← needed for std::vector
#include <cuda.h>
#include <cuda_runtime.h>

template<int K, int TX, int TY>
__global__ void unrolled_threadmapped_bwd_kernel(
    const float* __restrict__ input,      // [N, C_in, H_in, W_in]
    const float* __restrict__ weight,     // [C_out, C_in, K, K]
    const float* __restrict__ bias,       // [C_out]  (unused – kept for symmetry)
    const float* __restrict__ grad_out,   // [N, C_out, H_out, W_out]

    /* outputs */
    float* __restrict__ grad_input,       // [N, C_in, H_in, W_in]
    float* __restrict__ grad_weight,      // [C_out, C_in, K, K]
    float* __restrict__ grad_bias,        // [C_out]

    /* sizes */
    int N, int C_in, int C_out,
    int H_in, int W_in)
{
    /* ------------------------------------------------------------ *
     * Thread/block layout is IDENTICAL to the forward kernel:
     *   blockIdx.z = (n, oc) pair, one block per output channel-sample
     *   threadIdx.{x,y} = one pooled pixel (ow, oh) inside that tile
     * ------------------------------------------------------------ */
    constexpr int UNROLL = K;               // convenience
    int H_conv = H_in - K + 1;
    int W_conv = W_in - K + 1;
    int H_out  = H_conv >> 1;               // /2
    int W_out  = W_conv >> 1;

    /* which sample / output-channel does this block handle? */
    int z  = blockIdx.z;
    int n  = z / C_out;
    int oc = z % C_out;
    if (n >= N) return;

    /* top-left co-ordinate of the *pooled* tile handled by this block */
    int out_x0 = blockIdx.x * TX;
    int out_y0 = blockIdx.y * TY;

    /* ----------------------------------------------------------------
     * 1. Bring this block's kernel weights into shared memory
     * ----------------------------------------------------------------*/
    extern __shared__ float shm_w[];
    int WperThread = (C_in*K*K + TX*TY - 1) / (TX*TY);
    int tid = threadIdx.y*TX + threadIdx.x;

    for (int i=0; i<WperThread; ++i)
    {
        int idx = tid + i*TX*TY;
        if (idx < C_in*K*K)
            shm_w[idx] = weight[oc*C_in*K*K + idx];
    }
    __syncthreads();

    /* local indices inside this pooled tile */
    int tx = threadIdx.x;
    int ty = threadIdx.y;
    int ow = out_x0 + tx;
    int oh = out_y0 + ty;
    if (ow >= W_out || oh >= H_out)
        return;

    /* upstream gradient dL/dP (P = pooled output) */
    int out_idx = ((n*C_out + oc)*H_out + oh)*W_out + ow;
    float grad_pool = grad_out[out_idx];

    /* ----------------------------------------------------------------
     * 2. Recompute the *four* convolution outputs that fed the pool
     *    so we know which one “won” the 2×2 max.
     *    This costs extra FLOPs but avoids storing a pool-mask tensor.
     * ----------------------------------------------------------------*/
    float conv_val[4];          // order: (0,0) (0,1) (1,0) (1,1)
#pragma unroll
    for (int i=0; i<4; ++i) conv_val[i] = bias[oc];

#pragma unroll 2
    for (int ph=0; ph<2; ++ph)
    {
#pragma unroll 2
        for (int pw=0; pw<2; ++pw)
        {
            int idx = ph*2 + pw;                      // 0..3
            int cv_y = (oh<<1) + ph;                  // = oh*2+ph
            int cv_x = (ow<<1) + pw;

            for (int ic=0; ic<C_in; ++ic)
            {
                int base = ic*K*K;
#pragma unroll
                for (int ky=0; ky<K; ++ky)
                {
                    int in_y = cv_y + ky;
                    const float* row = input + ((n*C_in+ic)*H_in + in_y)*W_in;
#pragma unroll
                    for (int kx=0; kx<K; ++kx)
                    {
                        conv_val[idx] += row[cv_x+kx] * shm_w[base + ky*K + kx];
                    }
                }
            }

            /* ReLU inside the loop (strictly after accumulation) */
            conv_val[idx] = conv_val[idx] > 0.f ? conv_val[idx] : 0.f;
        }
    }

    /* ----------------------------------------------------------------
     * 3. Find which of the four positions was the *max* after ReLU
     * ----------------------------------------------------------------*/
    float  best_val = conv_val[0];
    int    best_idx = 0;
#pragma unroll
    for (int i=1; i<4; ++i)
        if (conv_val[i] > best_val)
        {
            best_val = conv_val[i];
            best_idx = i;
        }

    /* ----------------------------------------------------------------
     * 4. Back-prop through max-pool + ReLU.
     *    Only the winning location gets the upstream gradient,
     *    and ONLY if its ReLU output was >0.
     * ----------------------------------------------------------------*/
    float grad_conv[4] = {0,0,0,0};
    grad_conv[best_idx] = (best_val > 0.f) ? grad_pool : 0.f;

    /* bias gradient —  one add per block-thread is fine            */
    if (grad_conv[best_idx] != 0.f)
        atomicAdd(&grad_bias[oc], grad_conv[best_idx]);

    /* ----------------------------------------------------------------
     * 5. Propagate into (a) grad_input  (b) grad_weight
     *    Every thread touches up to K×K×C_in elements → atomics
     * ----------------------------------------------------------------*/
#pragma unroll 2
    for (int ph=0; ph<2; ++ph)
    {
#pragma unroll 2
        for (int pw=0; pw<2; ++pw)
        {
            float g = grad_conv[ph*2 + pw];
            if (g == 0.f) continue;

            int cv_y = (oh<<1) + ph;
            int cv_x = (ow<<1) + pw;

            for (int ic=0; ic<C_in; ++ic)
            {
                int base = ic*K*K;
#pragma unroll
                for (int ky=0; ky<K; ++ky)
                {
                    int in_y = cv_y + ky;
                    const float* in_row = input + ((n*C_in+ic)*H_in + in_y)*W_in;

#pragma unroll
                    for (int kx=0; kx<K; ++kx)
                    {
                        /* 5a. ⟵ dInput (atomic add) */
                        int in_idx = ((n*C_in + ic)*H_in + in_y)*W_in + (cv_x + kx);
                        atomicAdd(&grad_input[in_idx],
                                  g * shm_w[base + ky*K + kx]);

                        /* 5b. ⟵ dWeight (atomic add) */
                        int w_idx = ((oc*C_in + ic)*K + ky)*K + kx;
                        atomicAdd(&grad_weight[w_idx],
                                  g * in_row[cv_x + kx]);
                    }
                }
            }
        }
    }
}


std::vector<torch::Tensor> backward_cuda(
        torch::Tensor  input,
        torch::Tensor  weight,
        torch::Tensor  bias,
        torch::Tensor  grad_out)
{
    /* dimensions ---------------------------------------------------- */
    int N     = input.size(0);
    int C_in  = input.size(1);
    int H_in  = input.size(2);
    int W_in  = input.size(3);
    int C_out = weight.size(0);
    int K     = weight.size(2);

    int H_conv = H_in - K + 1;
    int W_conv = W_in - K + 1;
    int H_out  = H_conv >> 1;
    int W_out  = W_conv >> 1;

    /* outputs ------------------------------------------------------- */
    auto dInput  = torch::zeros_like(input);
    auto dWeight = torch::zeros_like(weight);
    auto dBias   = torch::zeros_like(bias);

    /* launch geometry ---------------------------------------------- */
    constexpr int TX = 16, TY = 8;
    dim3 threads(TX, TY);
    int gx = (W_out + TX - 1) / TX;
    int gy = (H_out + TY - 1) / TY;
    dim3 blocks(gx, gy, N*C_out);

    size_t shm = C_in * K * K * sizeof(float);

    /* pick the specialisation exactly like the forward pass -------- */
    if (K == 3)
    {
        unrolled_threadmapped_bwd_kernel<3,TX,TY><<<blocks,threads,shm>>>(
            input.data_ptr<float>(),
            weight.data_ptr<float>(),
            bias.data_ptr<float>(),
            grad_out.data_ptr<float>(),
            dInput.data_ptr<float>(),
            dWeight.data_ptr<float>(),
            dBias.data_ptr<float>(),
            N,C_in,C_out,H_in,W_in
        );
    }
    else   /* fallback assumes K==5 */
    {
        unrolled_threadmapped_bwd_kernel<5,TX,TY><<<blocks,threads,shm>>>(
            input.data_ptr<float>(),
            weight.data_ptr<float>(),
            bias.data_ptr<float>(),
            grad_out.data_ptr<float>(),
            dInput.data_ptr<float>(),
            dWeight.data_ptr<float>(),
            dBias.data_ptr<float>(),
            N,C_in,C_out,H_in,W_in
        );
    }

    /* return three tensors as a Python tuple */
    return {dInput, dWeight, dBias};
}

PYBIND11_MODULE(TORCH_EXTENSION_NAME, m)
{
    m.def("backward", &backward_cuda,
          "Unrolled thread-mapped fused Conv2d+ReLU+MaxPool2d backward (CUDA)");
}