# Running the simulator through a private Daytona broker

The optional `stabletoolbench.mirror_relay.environment:MirrorDaytonaEnvironment` backend lets a Daytona trial call a MirrorAPI-Cache service on the Harbor runner's loopback interface. Each call travels through the authenticated Daytona SDK to the current trial's Compose service named `broker`. The controller reads the generated task's `environment/broker/allowed-tools.json`, submits eligible requests to the fixed local `/virtual` endpoint, and records the requested API and returned observation in `mirror-relay.jsonl` inside the trial directory. These records may contain tool arguments and model outputs.

## Simulator

Run the original and Harbor evaluations against the same model, test tools and sampling settings. The validated configuration uses upstream StableToolBench at `aa4ed9f4737ad98bd706663f01d63623c3427812`, MirrorAPI-Cache at `df8f586d0f8d17bcbbaee11f1e8f9dd178a28c68` and the ToolBench test tool definitions described in the adapter README. [StableToolBench server](https://github.com/THUNLP-MT/StableToolBench/tree/aa4ed9f4737ad98bd706663f01d63623c3427812/server) · [MirrorAPI-Cache weights](https://huggingface.co/stabletoolbench/MirrorAPI-Cache/tree/df8f586d0f8d17bcbbaee11f1e8f9dd178a28c68)

Generate the five tasks from public sources in the adapter directory, following the [generation instructions](README.md#usage-create-task-directories). The generated broker directories contain the five verified tool JSON files. To build a common simulator tool folder from those files and fetch the same upstream server and weights, run the following once. Set `STB_ASSETS_DIR` to an absolute directory with room for the BF16 weights.

```sh
: "${STB_ASSETS_DIR:?Set an absolute asset directory before continuing}"
export STB_UPSTREAM_DIR="$STB_ASSETS_DIR/StableToolBench"
export STB_TOOLENV_DIR="$STB_ASSETS_DIR/toolenv/tools"
export MIRROR_MODEL_DIR="$STB_ASSETS_DIR/MirrorAPI-Cache"
mkdir -p "$STB_TOOLENV_DIR"
for task_tools in ./datasets/stabletoolbench/*/environment/broker/tools; do
  cp -R "$task_tools/." "$STB_TOOLENV_DIR/"
done

git init -q "$STB_UPSTREAM_DIR"
git -C "$STB_UPSTREAM_DIR" fetch --depth=1 \
  https://github.com/THUNLP-MT/StableToolBench.git aa4ed9f4737ad98bd706663f01d63623c3427812
git -C "$STB_UPSTREAM_DIR" checkout --detach FETCH_HEAD
hf download stabletoolbench/MirrorAPI-Cache \
  --revision df8f586d0f8d17bcbbaee11f1e8f9dd178a28c68 --local-dir "$MIRROR_MODEL_DIR"
```

Use the same private `VLLM_API_KEY` environment variable in both simulator shells. Select an available GPU with `CUDA_VISIBLE_DEVICES`. In a vLLM 0.10.2 environment, start the BF16 model service.

```sh
vllm serve "$MIRROR_MODEL_DIR" --served-model-name mirrorapi-cache \
  --host 127.0.0.1 --port 18181 --dtype bfloat16 --max-num-seqs 4 \
  --gpu-memory-utilization 0.85 --enforce-eager
```

In a separate Python 3.11 environment with the pinned upstream `server/requirements.txt` installed, start its FastAPI application. The temporary configuration is private to the process. The upstream endpoint submits chat completions with temperature 0.1, seed 42 and a 2,048-token output budget in this setup.

```sh
python - <<'PY'
import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
import uvicorn

server = Path(os.environ["STB_UPSTREAM_DIR"]).resolve() / "server"
tools = Path(os.environ["STB_TOOLENV_DIR"]).resolve()
sys.path.insert(0, str(server))
with tempfile.TemporaryDirectory() as runtime:
    os.chdir(runtime)
    config = {"api_base": "http://127.0.0.1:18181/v1", "api_key": os.environ["VLLM_API_KEY"],
              "tools_folder": str(tools), "model": "mirrorapi-cache", "temperature": 0.1, "port": 18180}
    Path("config_mirrorapi_cache.yml").write_text(json.dumps(config))
    # The upstream module prints its config during import.
    with open(os.devnull, "w") as silent, contextlib.redirect_stdout(silent):
        from main_mirrorapi_cache import app
    uvicorn.run(app, host="127.0.0.1", port=18180)
PY
```

## Harbor

From the adapter directory, use a Harbor Python environment with this adapter importable. The validated versions are Harbor 0.26.0 and Daytona SDK 0.233.0. Configure `DAYTONA_API_KEY` for the SDK and the three verifier-only Judge variables documented in the adapter README.

```sh
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
export STB_MIRROR_URL=http://127.0.0.1:18180/virtual
harbor run -e stabletoolbench.mirror_relay.environment:MirrorDaytonaEnvironment \
  -p ./datasets/stabletoolbench -a oracle -n 1
```

If the broker can reach the simulator directly, use the ordinary Harbor backend and set `STB_MIRROR_URL` to its broker-reachable address.
