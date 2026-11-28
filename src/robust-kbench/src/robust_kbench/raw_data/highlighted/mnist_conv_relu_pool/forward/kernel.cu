#include <torch/extension.h>
#include <cuda.h>
#include <cuda_runtime.h>

template<int K, int TX, int TY>
__global__ void unrolled_threadmapped_kernel(
    const float* __restrict__ input,   // [N, C_in, H_in, W_in]
    const float* __restrict__ weight,  // [C_out, C_in, K, K]
    const float* __restrict__ bias,    // [C_out]
    float* __restrict__ output,        // [N, C_out, H_out, W_out]
    int N, int C_in, int C_out,
    int H_in, int W_in) {

    int H_conv = H_in - K + 1, W_conv = W_in - K + 1;
    int H_out = H_conv >> 1, W_out = W_conv >> 1;

    int z = blockIdx.z;
    int n  = z / C_out;
    int oc = z % C_out;
    if (n >= N) return;

    int out_x0 = blockIdx.x * TX;
    int out_y0 = blockIdx.y * TY;

    extern __shared__ float shm_w[];
    int WperThread = (C_in*K*K + TX*TY - 1) / (TX*TY);
    int tid = threadIdx.y*TX + threadIdx.x;
    for(int i=0; i<WperThread; ++i){
        int idx = tid + i*TX*TY;
        if(idx < C_in*K*K){
            shm_w[idx] = weight[oc*C_in*K*K + idx];
        }
    }
    __syncthreads();

    int tx = threadIdx.x, ty = threadIdx.y;
    int ow = out_x0 + tx;
    int oh = out_y0 + ty;
    if(ow < W_out && oh < H_out){
        float best = -1e20f;
        #pragma unroll 2
        for(int ph=0; ph<2; ++ph){
            #pragma unroll 2
            for(int pw=0; pw<2; ++pw){
                int cv_y = (oh<<1)+ph;
                int cv_x = (ow<<1)+pw;
                float acc = bias[oc];
                for(int ic=0; ic<C_in; ++ic){
                    int base = ic*K*K;
                    #pragma unroll
                    for(int ky=0; ky<K; ++ky){
                        int in_y = cv_y+ky;
                        const float* row = input + ((n*C_in+ic)*H_in + in_y)*W_in;
                        #pragma unroll
                        for(int kx=0; kx<K; ++kx){
                            acc += row[cv_x + kx] * shm_w[base + ky*K + kx];
                        }
                    }
                }
                acc = acc>0.f? acc:0.f;
                best = acc>best? acc:best;
            }
        }
        int out_idx = ((n*C_out+oc)*H_out + oh)*W_out + ow;
        output[out_idx] = best;
    }
}

torch::Tensor forward_cuda(
    torch::Tensor input,
    torch::Tensor weight,
    torch::Tensor bias) {

    int N       = input.size(0);
    int C_in    = input.size(1);
    int H_in    = input.size(2);
    int W_in    = input.size(3);
    int C_out   = weight.size(0);
    int K       = weight.size(2);

    int H_conv  = H_in - K + 1;
    int W_conv  = W_in - K + 1;
    int H_out   = H_conv >> 1;
    int W_out   = W_conv >> 1;

    auto output = torch::empty({N, C_out, H_out, W_out}, input.options());

    constexpr int TX = 16, TY = 8;
    dim3 threads(TX, TY);
    int gx = (W_out + TX - 1)/TX;
    int gy = (H_out + TY - 1)/TY;
    dim3 blocks(gx, gy, N*C_out);

    size_t shm = C_in*K*K * sizeof(float);

    // Unroll for K=3 (as in MNIST)
    if (K == 3) {
        unrolled_threadmapped_kernel<3,TX,TY><<<blocks,threads,shm>>>(
            input.data_ptr<float>(),
            weight.data_ptr<float>(),
            bias.data_ptr<float>(),
            output.data_ptr<float>(),
            N,C_in,C_out,H_in,W_in
        );
    } else {
        // fallback: no unrolling
        unrolled_threadmapped_kernel<5,TX,TY><<<blocks,threads,shm>>>(
            input.data_ptr<float>(),
            weight.data_ptr<float>(),
            bias.data_ptr<float>(),
            output.data_ptr<float>(),
            N,C_in,C_out,H_in,W_in
        );
    }

    return output;
}

PYBIND11_MODULE(TORCH_EXTENSION_NAME, m) {
    m.def("forward", &forward_cuda, "Unrolled thread-mapped fused Conv2d+ReLU+MaxPool2d (CUDA)");
}