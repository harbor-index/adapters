"""Score native one-shot JSONL with the pinned Tencent worker in the cloud verifier image."""

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import pwd
import subprocess
import sys
import tempfile
from contextlib import contextmanager, redirect_stdout
from pathlib import Path

SRC = Path("/opt/artifactsbench/src")
NATIVE_SHA = "0949a4e09e41844e4eb9a9f59dcdeb697c8abfd4b156783e1d0d839ff6d5bdae"
TOKENIZER = "/opt/artifactsbench/tokenizer"


def load_native(path):
    if hashlib.sha256(path.read_bytes()).hexdigest() != NATIVE_SHA:
        raise ValueError("Unexpected Tencent infer_gemini.py revision")
    sys.path.insert(0, str(SRC))
    return load_module("artifactsbench_native", path)


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def send(stream, data):
    stream.write(json.dumps(data, ensure_ascii=False) + "\n")
    stream.flush()


def receive(stream):
    line = stream.readline()
    if not line:
        raise RuntimeError("Native worker communication ended before the row completed")
    return json.loads(line)


def run_worker(native, tokenizer, source, sink):
    # The process has no key. Only its make_request callback crosses to the
    # credential-bearing parent; the Tencent renderer, prompt and parser run here.
    def bridge(messages, api_key, model_marker, api_url, **kwargs):
        send(sink, {"messages": messages})
        return receive(source)["assistant_text"]

    native.make_request = bridge
    for line in source:
        row = json.loads(line)
        with tempfile.TemporaryDirectory() as screenshots:
            _, result = native.process_item(
                (0, row), 3, screenshots, tokenizer, "", "", "",
            )
        send(sink, {"result": result})


@contextmanager
def worker_process(native_path):
    account = pwd.getpwnam("pwuser")
    process = subprocess.Popen(
        [sys.executable, "-I", str(Path(__file__).resolve()), "--worker", str(native_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8",
        cwd="/tmp", user=account.pw_uid, group=account.pw_gid, extra_groups=[],
        env={"PATH": os.defpath, "LANG": "C.UTF-8", "HOME": account.pw_dir,
             "PLAYWRIGHT_BROWSERS_PATH": "/ms-playwright"},
    )
    try:
        yield process.stdin, process.stdout
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdout.close()


def unpack_messages(messages, expected_images):
    if len(messages) != 1 or messages[0]["role"] != "user":
        raise ValueError("Unexpected native judge messages")
    parts = messages[0]["content"]
    if len(parts) != 1 + expected_images or parts[0]["type"] != "text":
        raise ValueError("Native render did not supply the required visual evidence")
    images = []
    for part in parts[1:]:
        mime, encoded = part["value"].split(",", 1)
        if mime != "data:image/png;base64" or part["type"] != "image_url":
            raise ValueError("Unexpected native image format")
        image = base64.b64decode(encoded, validate=True)
        if not image.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Invalid native screenshot")
        images.append(image)
    return parts[0]["value"], images


def score_row(row, verifier, base_url, model, key, source, sink):
    # Do not accidentally reuse a stored judge decision on worker failure.
    row = {k: v for k, v in row.items() if k not in ("gemini_reason", "gemini_ans", "artifactsbench_judge")}
    if "answer" not in row or not isinstance(row["answer"], str):
        raise ValueError("Expected native generator JSONL with an answer field")
    answer = verifier.get_answer(row)
    expected_images = 3 if verifier.extract_html_or_svg(answer) else 0
    send(sink, row)
    message = receive(source)
    if "messages" not in message:
        raise RuntimeError(f"Native worker produced no judge request for index {row['index']}")
    prompt, images = unpack_messages(message["messages"], expected_images)
    score, reason, returned_model, response_id = verifier.judge(row["index"], prompt, images, base_url, model, key)
    send(sink, {"assistant_text": reason})
    result = receive(source).get("result")
    if not result or float(result["gemini_ans"]) != score:
        raise RuntimeError(f"Native parser did not reproduce the judge score for index {row['index']}")
    result["artifactsbench_judge"] = {
        "requested_model": model,
        "returned_model": returned_model,
        "response_id": response_id,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "screenshot_sha256": [hashlib.sha256(i).hexdigest() for i in images],
    }
    return result


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        sink = sys.stdout
        # The original worker writes diagnostics to stdout; reserve the original
        # pipe for framed JSON and leave diagnostics on stderr.
        with redirect_stdout(sys.stderr):
            native = load_native(Path(sys.argv[2]))
            tokenizer = native.AutoTokenizer.from_pretrained(TOKENIZER, local_files_only=True)
            run_worker(native, tokenizer, sys.stdin, sink)
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--native-worker", type=Path, default=SRC / "infer_gemini.py")
    parser.add_argument("--verifier", type=Path, default=Path("/tests/verify.py"))
    args = parser.parse_args()
    verifier = load_module("artifactsbench_verifier", args.verifier)
    key = os.environ.pop("ARTIFACTSBENCH_JUDGE_API_KEY", "")
    if not key:
        raise ValueError("ARTIFACTSBENCH_JUDGE_API_KEY is required for native scoring")
    base_url = os.environ.get("ARTIFACTSBENCH_JUDGE_BASE_URL") or verifier.DEFAULT_BASE_URL
    model = os.environ.get("ARTIFACTSBENCH_JUDGE_MODEL") or verifier.DEFAULT_MODEL
    if args.output.exists():
        raise FileExistsError(args.output)
    partial = args.output.with_name(args.output.name + ".partial")
    args.output.parent.mkdir(exist_ok=True, parents=True)
    seen = set()
    total = 0.0
    with args.input.open(encoding="utf-8") as rows, partial.open("x", encoding="utf-8") as out:
        with worker_process(args.native_worker.resolve()) as (sink, source):
            for row in map(json.loads, rows):
                if row["index"] in seen:
                    raise ValueError(f"Duplicate index {row['index']}")
                result = score_row(row, verifier, base_url, model, key, source, sink)
                send(out, result)
                seen.add(row["index"])
                total += float(result["gemini_ans"])
    if not seen:
        raise ValueError("No rows to score")
    os.link(partial, args.output)
    partial.unlink()
    print(json.dumps({"rows": len(seen), "mean_score_on_100": total / len(seen)}))


if __name__ == "__main__":
    main()
