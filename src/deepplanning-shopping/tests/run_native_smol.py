"""Run a local Smol trial with a scripted, loopback-only model endpoint."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True, type=Path)
    parser.add_argument("--jobs-dir", required=True, type=Path)
    parser.add_argument("--job-name", default="native-offline-smol")
    args = parser.parse_args()
    args.jobs_dir.mkdir(parents=True, exist_ok=True)
    fixture = args.jobs_dir / (args.job_name + "-fixture")
    fixture.mkdir()
    reference = json.loads((args.task / "solution/reference.json").read_text())

    def call(identifier, name, arguments):
        return {
            "id": identifier,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }

    responses = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [call("profile", "get_user_info", {})],
        },
        {"role": "assistant", "content": "I have a plan."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                call(
                    f"add-{i}",
                    "add_product_to_cart",
                    {"product_id": product["product_id"], "quantity": 1},
                )
                for i, product in enumerate(reference["ground_truth_products"])
            ]
            + [call("cart", "get_cart_info", {})],
        },
        {"role": "assistant", "content": "The requested items are in the cart."},
    ]
    (fixture / "responses.json").write_text(json.dumps(responses))
    (fixture / "models_config.json").write_text(
        json.dumps(
            {
                "models": {
                    "offline-model": {
                        "model_name": "offline-model",
                        "model_type": "openai",
                        "base_url": "http://127.0.0.1:18741/v1",
                        "api_key_env": "DEEPPLANNING_TEST_KEY",
                        "max_retries": 1,
                    }
                }
            }
        )
    )
    env = dict(
        os.environ,
        DEEPPLANNING_TEST_KEY="offline-test-only",
        LITELLM_LOCAL_MODEL_COST_MAP="True",
    )
    env["PYTHONPATH"] = str(Path(__file__).parent.resolve())
    subprocess.run(
        [
            sys.executable,
            "-m",
            "harbor.cli.main",
            "run",
            "-p",
            str(args.task.parent.resolve()),
            "-i",
            args.task.name,
            "-a",
            "offline_native_agent:OfflineShoppingFnAgent",
            "-m",
            "offline-model",
            "--ak",
            f"model_config_path={fixture.resolve() / 'models_config.json'}",
            "--ak",
            f"responses_path={fixture.resolve() / 'responses.json'}",
            "-e",
            "smol",
            "--ek",
            "target=local",
            "--ek",
            "auto_checkpoint=false",
            "-n",
            "1",
            "--jobs-dir",
            str(args.jobs_dir.resolve()),
            "--job-name",
            args.job_name,
        ],
        env=env,
        check=True,
    )
    job = args.jobs_dir / args.job_name
    trials = list(job.glob("*/result.json"))
    assert len(trials) == 1
    result = json.loads(trials[0].read_text())
    assert result["exception_info"] is None, result["exception_info"]
    assert result["verifier_environment_mode"] == "separate"
    assert result["verifier_result"]["rewards"]["reward"] == 1.0
    trial = trials[0].parent
    requests = [
        json.loads(line)
        for line in (trial / "agent/offline-requests.jsonl").read_text().splitlines()
    ]
    prompt = json.loads((args.task / "environment/prompt.json").read_text())
    assert len(requests) == 4
    assert requests[0]["messages"] == [
        {"role": "system", "content": prompt["system"]},
        {"role": "user", "content": prompt["query"]},
    ]
    assert requests[0]["tools"] == json.loads(
        (args.task / "environment/tools/shopping_tool_schema.json").read_text()
    )
    assert (
        "Check whether the items in the shopping cart"
        in requests[2]["messages"][-1]["content"]
    )
    receipt = {
        "test": "scripted native-agent integration; no model parity claim",
        "task": result["task_name"],
        "reward": 1.0,
        "exceptions": 0,
        "native_client_requests": 4,
        "inference_endpoint": "loopback only",
        "external_connections": "blocked by fixture",
        "paid_api_calls": 0,
        "verifier_environment_mode": "separate",
    }
    (job / "offline-validation.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
