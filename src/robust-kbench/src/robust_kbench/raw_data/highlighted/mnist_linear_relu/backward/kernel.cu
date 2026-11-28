#include <torch/extension.h>
#include <cuda.h>
#include <cuda_runtime.h>
#include <vector>

#define CHECK_TENSOR(x) TORCH_CHECK(x.is_cuda() && x.is_contiguous(), #x " must be a contiguous CUDA tensor")

// ------------------------------------------------------------------------------------------------
// kernel 1 : build masked gradient and bias gradient
// ------------------------------------------------------------------------------------------------
__global__ void masked_grad_bias_kernel(const float *__restrict__ x,
                                        const float *__restrict__ w,
                                        const float *__restrict__ bias,
                                        const float *__restrict__ grad_out,
                                        float *__restrict__ masked_grad,
                                        float *__restrict__ grad_bias,
                                        const int batch,
                                        const int in_feat,
                                        const int out_feat)
{
    const int o = blockIdx.x;                                // output neuron handled by this block
    const int tid = threadIdx.x;
    const int stride = blockDim.x;

    for (int b = tid; b < batch; b += stride)
    {
        // dot( x[b], w[o] )
        float acc = 0.f;
        const float *x_ptr = x + b * in_feat;
        const float *w_ptr = w + o * in_feat;
#pragma unroll
        for (int i = 0; i < in_feat; ++i)
            acc += x_ptr[i] * w_ptr[i];

        acc += bias[o];                                      // + b

        const float go = grad_out[b * out_feat + o];
        const float mg = (acc > 0.f) ? go : 0.f;             // apply mask

        masked_grad[b * out_feat + o] = mg;                  // store masked gradient

        atomicAdd(grad_bias + o, mg);                        // accumulate bias gradient
    }
}

// ------------------------------------------------------------------------------------------------
// kernel 2 : weight gradient  (dL/dW = masked_grad^T @ x)
// one block  -> one output neuron
// one thread -> several input features (looped by stride if needed)
// ------------------------------------------------------------------------------------------------
__global__ void grad_weights_kernel(const float *__restrict__ x,
                                    const float *__restrict__ masked_grad,
                                    float *__restrict__ grad_w,
                                    const int batch,
                                    const int in_feat,
                                    const int out_feat)
{
    const int o = blockIdx.x;               // output neuron for this block
    const int tid = threadIdx.x;
    const int stride = blockDim.x;

    for (int i = tid; i < in_feat; i += stride)
    {
        float acc = 0.f;
        for (int b = 0; b < batch; ++b)
            acc += x[b * in_feat + i] * masked_grad[b * out_feat + o];

        grad_w[o * in_feat + i] = acc;      // write gradient
    }
}

// ------------------------------------------------------------------------------------------------
// kernel 3 : input gradient (dL/dx = masked_grad @ W)
// one block  -> one sample
// one thread -> several input features (looped by stride if needed)
// ------------------------------------------------------------------------------------------------
__global__ void grad_input_kernel(const float *__restrict__ masked_grad,
                                  const float *__restrict__ w,
                                  float *__restrict__ grad_in,
                                  const int batch,
                                  const int in_feat,
                                  const int out_feat)
{
    const int b = blockIdx.x;               // sample handled by this block
    const int tid = threadIdx.x;
    const int stride = blockDim.x;

    for (int i = tid; i < in_feat; i += stride)
    {
        float acc = 0.f;
        const float *w_col = w + i;         // column i across all out neurons
#pragma unroll
        for (int o = 0; o < out_feat; ++o)
            acc += masked_grad[b * out_feat + o] *
                   w_col[o * in_feat];

        grad_in[b * in_feat + i] = acc;
    }
}

// ------------------------------------------------------------------------------------------------
// Host dispatcher
// ------------------------------------------------------------------------------------------------
std::vector<torch::Tensor> backward(torch::Tensor grad_out,
                                    torch::Tensor x,
                                    torch::Tensor weight,
                                    torch::Tensor bias)
{
    CHECK_TENSOR(grad_out);
    CHECK_TENSOR(x);
    CHECK_TENSOR(weight);
    CHECK_TENSOR(bias);

    const int batch      = x.size(0);
    const int in_feat    = x.size(1);
    const int out_feat   = weight.size(0);

    auto grad_input   = torch::empty_like(x);
    auto grad_weight  = torch::empty_like(weight);
    auto grad_bias    = torch::zeros_like(bias);          // zero-ed for atomic adds
    auto masked_grad  = torch::empty_like(grad_out);      // temporary buffer

    // launch parameters
    constexpr int THREADS = 256;

    // 1) masked grad + bias grad
    masked_grad_bias_kernel<<<out_feat, THREADS>>>(x.data_ptr<float>(),
                                                   weight.data_ptr<float>(),
                                                   bias.data_ptr<float>(),
                                                   grad_out.data_ptr<float>(),
                                                   masked_grad.data_ptr<float>(),
                                                   grad_bias.data_ptr<float>(),
                                                   batch,
                                                   in_feat,
                                                   out_feat);

    // 2) weight grad
    grad_weights_kernel<<<out_feat, THREADS>>>(x.data_ptr<float>(),
                                               masked_grad.data_ptr<float>(),
                                               grad_weight.data_ptr<float>(),
                                               batch,
                                               in_feat,
                                               out_feat);

    // 3) input grad
    grad_input_kernel<<<batch, THREADS>>>(masked_grad.data_ptr<float>(),
                                          weight.data_ptr<float>(),
                                          grad_input.data_ptr<float>(),
                                          batch,
                                          in_feat,
                                          out_feat);

    return {grad_input, grad_weight, grad_bias};
}

PYBIND11_MODULE(TORCH_EXTENSION_NAME, m)
{
    m.def("backward", &backward, "Fused Linear+ReLU backward (x @ W^T + b)");
}