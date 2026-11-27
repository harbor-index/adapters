"""Generate native ArtifactsBench answers with the same one-turn request as Harbor."""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .adapter import ArtifactsBenchAdapter


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _complete(payload):
    base_url = os.environ["ARTIFACTSBENCH_GENERATOR_BASE_URL"].rstrip("/")
    url = urlsplit(base_url)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("Expected an HTTPS generator base URL without credentials, query, or fragment")
    key = os.environ["ARTIFACTSBENCH_GENERATOR_API_KEY"]
    if not key:
        raise ValueError("ARTIFACTSBENCH_GENERATOR_API_KEY is empty")
    request = Request(
        base_url + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with build_opener(_NoRedirect()).open(request, timeout=900) as response:
            return json.load(response)
    except HTTPError as error:
        raise RuntimeError(f"Generator returned HTTP {error.code}") from None


def generate(question, model):
    if not model:
        raise ValueError("Supply a generator model ID")
    settings = {"model": model}
    temperature = os.environ.get("ARTIFACTSBENCH_GENERATOR_TEMPERATURE")
    if temperature:
        value = float(temperature)
        if not math.isfinite(value) or not 0 <= value <= 2:
            raise ValueError("Temperature must be in [0, 2]")
        settings["temperature"] = value
    limit = os.environ.get("ARTIFACTSBENCH_GENERATOR_MAX_COMPLETION_TOKENS")
    if limit:
        value = int(limit)
        if value < 1:
            raise ValueError("Max completion tokens must be positive")
        settings["max_completion_tokens"] = value
    payload = {**settings, "messages": [{"role": "user", "content": question}]}
    result = _complete(payload)
    choice = result["choices"][0]
    if choice.get("finish_reason") not in ("stop", "length"):
        raise RuntimeError(f"Generator did not finish normally: {choice.get('finish_reason')!r}")
    answer = choice["message"]["content"]
    if not isinstance(answer, str):
        raise ValueError("Generator response does not contain a text answer")
    metadata = {
        "settings": settings,
        "question_sha256": hashlib.sha256(question.encode("utf-8")).hexdigest(),
        "generator_base_url": os.environ["ARTIFACTSBENCH_GENERATOR_BASE_URL"].rstrip("/"),
        "returned_model": result.get("model"),
        "response_id": result.get("id"),
        "finish_reason": choice["finish_reason"],
        "usage": result.get("usage"),
    }
    return answer, metadata


def generate_original(dataset, task_ids, model, output):
    # Use the same release hash, ID validation, and source order as the task generator.
    records = ArtifactsBenchAdapter(output.parent, dataset=dataset, task_ids=task_ids)._records()
    if output.exists():
        raise FileExistsError(f"Output already exists: {output}")
    output.parent.mkdir(exist_ok=True, parents=True)
    partial = output.with_name(output.name + ".partial")
    # A failed run keeps completed rows in .partial. Use a new output name for
    # the remaining IDs. Only a fully written cohort appears at the requested path.
    with partial.open("x", encoding="utf-8") as stream:
        for row in records:
            answer, metadata = generate(row["question"], model)
            entry = dict(row, answer=answer, artifactsbench_generation=metadata)
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
            stream.flush()
    os.link(partial, output)
    partial.unlink()
    return len(records)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, help="Local copy of the pinned release, otherwise downloaded")
    parser.add_argument("--task-ids", required=True, type=int, nargs="+")
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    count = generate_original(args.dataset, args.task_ids, args.model, args.output)
    print(f"Wrote {count} single-turn answers to {args.output}")


if __name__ == "__main__":
    main()
