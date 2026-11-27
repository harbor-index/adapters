"""Run the pinned Tencent evaluator with a multimodal chat-completions transport."""

import base64
import hashlib
import json
import os
import pwd
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests
from transformers import AutoTokenizer

sys.path.insert(0, "/opt/artifactsbench/src")
from extract_ans import extract_mllm_overall
from prompts.prompt_mllm_check import get_prompt_mllm_checklist
from utils import extract_html_content, extract_html_or_svg, get_answer, get_query, truncate_content

LOGS = Path("/logs/verifier")
RECORD = Path("/tests/record.json")
SUBMISSION = Path("/workspace/answer.md")
TOKENIZER = "/opt/artifactsbench/tokenizer"
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
DEFAULT_MODEL = "gemini-2.5-pro"


def render(answer):
    extracted = extract_html_or_svg(answer)
    if not extracted:
        return []

    # Run candidate-controlled markup under a distinct OS user. Do not pass
    # the parent environment (which contains the judge key) to the browser.
    account = pwd.getpwnam("pwuser")
    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        page = work / "artifact.html"
        page.write_text(extract_html_content(answer, extracted), encoding="utf-8")
        os.chown(work, account.pw_uid, account.pw_gid)
        paths = [work / f"screenshot-{i}.png" for i in (1, 2, 3)]
        subprocess.run(
            [sys.executable, "-I", "/tests/render.py", str(page), *map(str, paths)],
            cwd=directory,
            user=account.pw_uid,
            group=account.pw_gid,
            extra_groups=[],
            env={
                "PATH": os.defpath,
                "HOME": account.pw_dir,
                "LANG": "C.UTF-8",
                "PLAYWRIGHT_BROWSERS_PATH": "/ms-playwright",
            },
            check=True,
        )
        images = [p.read_bytes() for p in paths]
        if not all(data.startswith(b"\x89PNG\r\n\x1a\n") for data in images):
            raise RuntimeError("Incomplete Chromium screenshot evidence")
        return images


def judge(index, prompt, images, base_url, model, key):
    content = [{"type": "text", "text": prompt}]
    for image in images:
        encoded = base64.b64encode(image).decode("ascii")
        image_url = {"url": f"data:image/png;base64,{encoded}"}
        content.append({"type": "image_url", "image_url": image_url})
    payload = {"model": model, "messages": [{"role": "user", "content": content}]}
    url = base_url.rstrip("/") + "/chat/completions"
    for attempt in range(4):
        try:
            response = requests.post(
                url,
                headers={"Authorization": f"Bearer {key}"},
                json=payload,
                timeout=(15, 180),
                allow_redirects=False,
            )
            if response.status_code in (408, 429) or response.status_code >= 500:
                raise RuntimeError(f"Judge returned retryable HTTP {response.status_code}")
            if not 200 <= response.status_code < 300:
                # Some gateways include credential details in error response bodies.
                raise ValueError(f"Judge returned HTTP {response.status_code}")
            result = response.json()
            returned_model = result.get("model") or ""
            if returned_model.rsplit("/", 1)[-1] != model.rsplit("/", 1)[-1]:
                raise ValueError(f"Judge identity mismatch: requested {model!r}, got {returned_model!r}")
            text = result["choices"][0]["message"]["content"]
            if not isinstance(text, str):
                raise RuntimeError("Judge returned no text")
            reason = text.split("</think>")[-1].strip()
            score = extract_mllm_overall(index, reason)
            if score is None or not 0 <= float(score) <= 100:
                raise RuntimeError("Judge returned no valid overall score in [0, 100]")
            return float(score), reason, returned_model, result.get("id")
        except (requests.RequestException, RuntimeError, KeyError, IndexError, TypeError) as error:
            if attempt == 3:
                raise RuntimeError("Judge did not produce a score after four attempts") from error
            time.sleep(2**attempt)


def main():
    reward = Path(sys.argv[1]) if len(sys.argv) > 1 else LOGS / "reward.txt"
    logs = reward.parent
    logs.mkdir(exist_ok=True, parents=True)
    for old_file in (reward, logs / "judge.json", logs / "inputs.json"):
        old_file.unlink(missing_ok=True)
    key = os.environ.pop("ARTIFACTSBENCH_JUDGE_API_KEY", "")
    base_url = os.environ.get("ARTIFACTSBENCH_JUDGE_BASE_URL") or DEFAULT_BASE_URL
    model = os.environ.get("ARTIFACTSBENCH_JUDGE_MODEL") or DEFAULT_MODEL

    record = json.loads(RECORD.read_text(encoding="utf-8"))
    if SUBMISSION.is_symlink() or (SUBMISSION.exists() and not SUBMISSION.is_file()):
        raise ValueError("Expected a regular submission file")
    raw = SUBMISSION.read_bytes().decode("utf-8") if SUBMISSION.is_file() else ""
    answer = get_answer({"answer": raw})
    images = render(answer)
    tokenizer = AutoTokenizer.from_pretrained(TOKENIZER, local_files_only=True)
    text = get_prompt_mllm_checklist(record["checklist"], get_query(record), answer)
    prompt = truncate_content(text, tokenizer)

    evidence = {
        "index": record["index"],
        "submission_present": SUBMISSION.is_file(),
        "answer_sha256": hashlib.sha256(answer.encode()).hexdigest(),
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "screenshot_sha256": [hashlib.sha256(image).hexdigest() for image in images],
        "requested_model": model,
    }
    (logs / "inputs.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    if not key:
        raise RuntimeError("ARTIFACTSBENCH_JUDGE_API_KEY is required to produce a score")
    score, reason, returned_model, response_id = judge(record["index"], prompt, images, base_url, model, key)
    result = dict(
        evidence,
        gemini_ans=score,
        gemini_reason=reason,
        returned_model=returned_model,
        response_id=response_id,
    )
    (logs / "judge.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    pending = logs / "reward.tmp"
    pending.write_text(f"{score / 100}\n", encoding="utf-8")
    pending.replace(reward)


if __name__ == "__main__":
    main()
