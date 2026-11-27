"""Compare a real Harbor inputs.json to Tencent's native worker on reviewed sample 73.

Run in the cloud verifier image. Mount infer_gemini.py from Tencent revision
88c968b87e150e63de7660937e6dcfb8e7d643cf. No judge service is contacted.
"""

import argparse
import base64
import hashlib
import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch


UPSTREAM_SRC = Path("/opt/artifactsbench/src")
TOKENIZER = "/opt/artifactsbench/tokenizer"
FILE_DIGESTS = {
    "code_parser.py": "eabdc002c5f2c7eba4a8b7c130dbb36f0c75dfc5b890c2ea963c985d96f7db44",
    "extract_ans.py": "6ee46429496d41a77d08584dfababfafd84b1b567a13f753d563578a9b18c090",
    "utils.py": "ef3ffe4de9836d359ea05601651013815ceee7451a80502bd94dc8f8e9032858",
    "prompts/prompt_mllm_check.py": "6e2188edd37f17abc1b7f704bfc41daa5241794cf7e2c328a3a8398797e9f3c0",
}
NATIVE_DIGEST = "0949a4e09e41844e4eb9a9f59dcdeb697c8abfd4b156783e1d0d839ff6d5bdae"
ANSWER_DIGEST = "ce5eeb7b5fd0c8851b4d91382e4ec5ca1ccb17ae06f005c7358739abd22bd9f4"
RECORD_DIGEST = "26474b88ef4ca946dad9525a0ac80fa5c398da16b09b5b34f5b497712728a902"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def compare(args):
    for name, expected in FILE_DIGESTS.items():
        if digest((UPSTREAM_SRC / name).read_bytes()) != expected:
            raise ValueError(f"Different upstream evaluator file: {name}")
    if digest(args.native_worker.read_bytes()) != NATIVE_DIGEST:
        raise ValueError("Different upstream infer_gemini.py")
    record = json.loads(args.record.read_bytes())
    canonical = json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    if digest(canonical.encode()) != RECORD_DIGEST or digest(args.answer.read_bytes()) != ANSWER_DIGEST:
        raise ValueError("This diagnostic only renders the reviewed response and record for task 73")
    harbor_inputs = json.loads(args.harbor_inputs.read_bytes())
    sys.path.insert(0, str(UPSTREAM_SRC))
    native = load_module("infer_gemini", args.native_worker)
    verifier = load_module("harbor_artifactsbench_verifier", args.verifier)
    tokenizer = native.AutoTokenizer.from_pretrained(TOKENIZER, local_files_only=True)

    # Inject at the HTTP boundary so that both real payload constructors and
    # both real response parsers run. No API key or HTTP service is needed.
    model = harbor_inputs["requested_model"]
    fixture = '<think>transport fixture</think>{"Overall Score": "37.5"}'
    stub = Mock()
    stub.json.return_value = {"answer": [{"value": fixture}]}
    record["model_infer_result"] = args.answer.read_bytes().decode("utf-8")
    with tempfile.TemporaryDirectory() as work, patch.object(native.requests, "post", return_value=stub) as post:
        _, row = native.process_item((0, record), 3, work, tokenizer, "fixture", model, "memory://judge")
    if post.call_count != 1:
        raise RuntimeError(f"Native worker attempted {post.call_count} requests")
    content = post.call_args.kwargs["json"]["messages"][0]["content"]
    if [part["type"] for part in content] != ["text", "image_url", "image_url", "image_url"]:
        raise ValueError("Native worker did not produce text and three images")
    prompt = content[0]["value"]
    images = []
    for item in content[1:]:
        mime, data = item["value"].split(",", 1)
        if mime != "data:image/png;base64":
            raise ValueError("Expected native PNG")
        images.append(base64.b64decode(data, validate=True))
    native_inputs = {
        "index": row["index"],
        "answer_sha256": digest(native.get_answer(row).encode()),
        "prompt_sha256": digest(prompt.encode()),
        "screenshot_sha256": [digest(image) for image in images],
    }
    matches = {key: value == harbor_inputs.get(key) for key, value in native_inputs.items()}
    harbor_stub = Mock(status_code=200)
    harbor_stub.json.return_value = {"model": model, "choices": [{"message": {"content": fixture}}]}
    with patch.object(verifier.requests, "post", return_value=harbor_stub) as harbor_post:
        score, _, _, _ = verifier.judge(73, prompt, images, "memory://judge", model, "fixture")
    parts = harbor_post.call_args.kwargs["json"]["messages"][0]["content"]
    matches["transport_content"] = (
        parts == [{"type": "text", "text": prompt}] + [
            {"type": "image_url", "image_url": {"url": item["value"]}} for item in content[1:]
        ]
    )
    matches["fixture_score_parse"] = float(row["gemini_ans"]) == score == 37.5
    return {
        "scope": "single reviewed static SVG; fixed in-memory response; no measured model score or parity",
        "native_inputs": native_inputs,
        "harbor_inputs": {key: harbor_inputs.get(key) for key in native_inputs},
        "matches": matches,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-worker", type=Path, required=True)
    parser.add_argument("--record", type=Path, default=Path("/tests/record.json"))
    parser.add_argument("--answer", type=Path, required=True)
    parser.add_argument("--harbor-inputs", type=Path, required=True)
    parser.add_argument("--verifier", type=Path, default=Path("/tests/verify.py"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = compare(args)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if all(report["matches"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
