# The adapter prepends upstream's src/harbor_adapter/template/Dockerfile, which mirrors
# containers/inference.def, to this file when it generates a task.

# LongBench-v2 has exactly 503 rows and the benchmark asks for 503, so the cached file is
# the same for every seed (the seed selects requests at evaluation time). It is 465 MB, so
# it is downloaded once in a layer that all tasks share instead of being stored in each
# task. The checksums fail the build if the upstream dataset changes.
COPY inference_eval /opt/inference_eval
RUN cd /opt && HF_HOME=/opt/tmp/hf_samples HF_HUB_CACHE=/opt/tmp/hf_samples/hub \
    HUGGINGFACE_HUB_CACHE=/opt/tmp/hf_samples/hub XDG_CACHE_HOME=/opt/tmp/hf_samples/xdg \
    python3 -m inference_eval.cache_samples \
        --seed {quality_seed} --mmlupro-n {quality_n} --longbench-n {longbench_n} && \
    rm -rf /opt/tmp/hf_samples && \
    cd /opt/inference_eval/baselines/samples && \
    echo "{longbench_sha256}  longbench_v2/{quality_seed}_{longbench_n}/samples.jsonl" | sha256sum -c - && \
    echo "{mmlu_sha256}  mmlu_pro/{quality_seed}_{quality_n}/samples.jsonl" | sha256sum -c - && \
    chmod -R a+rX,go-w /opt/inference_eval && \
    chmod a+x /opt/inference_eval/bin/launch_supervised_server.sh

# Harbor runs the collect hook and the verifier as root in this sandbox and resolves commands
# through the image's PATH, so the agent-writable bin directory must come last. The agent's own
# shell and the evaluation server still get it first (Harbor's agents and test.sh prepend it).
ENV PATH="/usr/local/nvidia/bin:/usr/local/cuda/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/root/.local/bin:/home/agent/.local/bin"

COPY collect.sh capture_runtime_env.py /usr/local/bin/
COPY home /home/agent
RUN if [ "{agent_seed}" != "{quality_seed}" ]; then \
        mkdir -p /opt/inference_eval/baselines/samples/longbench_v2/{agent_seed}_{longbench_n} && \
        ln -s ../{quality_seed}_{longbench_n}/samples.jsonl \
            /opt/inference_eval/baselines/samples/longbench_v2/{agent_seed}_{longbench_n}/samples.jsonl; \
    fi && \
    chmod +x /home/agent/task/*.sh && chown -R agent:agent /home/agent
WORKDIR /home/agent/task
