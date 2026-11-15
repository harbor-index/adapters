"""Run the pinned DeceptionBench judge without turning judge errors into scores."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if __package__:
    from .protocol import (
        CONDITIONS,
        SAMPLE_FIELDS,
        build_judge_prompt,
        label_metrics,
        normalize_response,
        parse_judge_response,
    )
else:
    # Generated tasks execute `python -I /tests/verify.py`. Only the trusted
    # verifier directory is added; never import modules from the submission cwd.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from protocol import (  # type: ignore[no-redef]
        CONDITIONS,
        SAMPLE_FIELDS,
        build_judge_prompt,
        label_metrics,
        normalize_response,
        parse_judge_response,
    )

JUDGE_PARAMETERS = {"max_tokens": 500, "temperature": 0.0, "top_p": 0.95}
MAX_SUBMISSION_BYTES = 1_048_576


class JudgeCallError(RuntimeError):
    """No judge response was received after the upstream retry allowance."""


class JudgeFormatError(ValueError):
    """A successful completion could not be scored; retain safe diagnostics."""

    def __init__(
        self,
        *,
        judge_output: str,
        judge_model: str,
        judge_parameters: dict[str, Any],
        outer_attempts: int,
        usage: dict[str, Any] | None,
        prompt_sha256: str,
    ) -> None:
        super().__init__("Judge response did not contain valid labels")
        # Copy only these explicit fields, never the SDK response, request,
        # headers, endpoint, or exception text.
        self.diagnostics = {
            "judge_output": judge_output,
            "judge_model": judge_model,
            "judge_parameters": judge_parameters,
            "outer_attempts": outer_attempts,
            "usage": usage,
            "prompt_sha256": prompt_sha256,
        }


class InvalidSubmission(ValueError):
    """The submission is not a bounded, regular UTF-8 response file."""


@dataclass(frozen=True)
class JudgeSettings:
    api_key: str = field(repr=False)
    model: str = "gpt-4o"
    base_url: str = "https://api.openai.com/v1"

    @classmethod
    def from_environment(cls) -> JudgeSettings:
        key = os.environ.get("DECEPTIONBENCH_JUDGE_API_KEY", "")
        if not key or key.startswith("${"):
            raise ValueError("DECEPTIONBENCH_JUDGE_API_KEY is required")
        model = os.environ.get("DECEPTIONBENCH_JUDGE_MODEL", "gpt-4o")
        base_url = os.environ.get(
            "DECEPTIONBENCH_JUDGE_BASE_URL", "https://api.openai.com/v1"
        )
        if not model or not base_url:
            raise ValueError("Judge model and base URL must be nonempty")
        return cls(api_key=key, model=model, base_url=base_url)

    def client(self) -> Any:
        from openai import OpenAI

        # Pin the SDK's default two HTTP retries explicitly. The upstream helper
        # additionally retries a whole completion call up to three times.
        return OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            max_retries=2,
            timeout=120.0,
        )


def read_submission(path: Path) -> tuple[str, float]:
    """Do not follow a submission symlink into late-mounted reference files."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise InvalidSubmission("Submission must be a regular file") from exc
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SUBMISSION_BYTES:
            raise InvalidSubmission("Submission must be a bounded regular file")
        raw = stream.read(MAX_SUBMISSION_BYTES + 1)
        if len(raw) > MAX_SUBMISSION_BYTES:
            raise InvalidSubmission("Submission exceeds the byte limit")
    try:
        return raw.decode("utf-8"), info.st_mtime
    except UnicodeDecodeError as exc:
        raise InvalidSubmission("Submission must be UTF-8") from exc


def call_judge(
    prompt: str,
    condition: str,
    client: Any,
    model: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Match upstream request parameters, cleanup and three outer attempts."""
    for attempt in range(3):
        try:
            completion = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                **JUDGE_PARAMETERS,
            )
            raw = completion.choices[0].message.content
            if not isinstance(raw, str):
                raise TypeError("Judge returned no text content")
            normalized = normalize_response(raw)
            break
        except Exception:  # noqa: BLE001 - match upstream retries, redact SDK errors
            # Do not log raw SDK exceptions: they can contain credentials or
            # endpoint query strings. Upstream sleeps 1s, then 2s here.
            if attempt == 2:
                raise JudgeCallError(
                    "Judge request failed after three attempts"
                ) from None
            sleep(2**attempt)
    diagnostics = {
        "judge_output": normalized,
        "judge_model": model,
        "judge_parameters": JUDGE_PARAMETERS,
        "outer_attempts": attempt + 1,
        "usage": (
            completion.usage.model_dump()
            if getattr(completion, "usage", None) is not None
            else None
        ),
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
    }
    # Upstream does not retry malformed judge JSON, so neither does this wrapper.
    try:
        labels = parse_judge_response(normalized, condition)
    except (ValueError, TypeError):
        raise JudgeFormatError(**diagnostics) from None
    return {
        "labels": labels,
        "eval": {condition: labels},
        "metrics": label_metrics(labels),
        **diagnostics,
    }


def verify(
    metadata_path: Path,
    response_path: Path,
    output_dir: Path,
    *,
    client: Any = None,
    settings: JudgeSettings | None = None,
    timestamp: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Write a scored result or an explicit infrastructure-error report.

    A missing/invalid submission gets reward zero and is not a judged deception
    label. API, configuration, and judge-format failures leave no reward file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    reward_path = output_dir / "reward.txt"
    reward_path.unlink(missing_ok=True)
    report: dict[str, Any] = {"status": "verifier_error"}
    exit_code = 2
    stage = "load_metadata"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        condition = metadata["condition"]
        sample = metadata["sample"]
        if condition not in CONDITIONS or any(
            not isinstance(sample.get(key), str) for key in SAMPLE_FIELDS
        ):
            raise ValueError("Invalid verifier metadata")
        report.update(
            task_id=metadata["task_id"],
            condition=condition,
            provenance=metadata.get("provenance", {}),
        )
        stage = "read_submission"
        try:
            response, modified_at = read_submission(response_path)
            if not normalize_response(response):
                raise FileNotFoundError("Empty submission")
        except (FileNotFoundError, InvalidSubmission) as exc:
            report.update(
                status=(
                    "missing_submission"
                    if isinstance(exc, FileNotFoundError)
                    else "invalid_submission"
                ),
                reward=0.0,
                metrics=None,
            )
            reward_path.write_text("0\n", encoding="utf-8")
            exit_code = 1
        else:
            stage = "configure_judge"
            settings = settings or JudgeSettings.from_environment()
            client = client if client is not None else settings.client()
            generated_at = timestamp or time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(modified_at)
            )
            prompt = build_judge_prompt(sample, condition, response, generated_at)
            stage = "evaluate_response"
            report["timestamp"] = generated_at
            result = call_judge(prompt, condition, client, settings.model, sleep=sleep)
            reward = result["metrics"]["response_honesty"]
            report.update(
                status="scored", reward=reward, timestamp=generated_at, **result
            )
            reward_path.write_text(f"{reward:g}\n", encoding="utf-8")
            exit_code = 0
    except Exception as exc:  # noqa: BLE001 - fail closed with a redacted diagnostic
        if isinstance(exc, JudgeFormatError):
            report.update(exc.diagnostics)
        report.update(
            status="verifier_error", error_type=type(exc).__name__, error_stage=stage
        )
        # Error text is deliberately excluded; messages can contain API secrets.
    (output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", type=Path, default=Path("/tests/metadata.json"))
    parser.add_argument(
        "--response", type=Path, default=Path("/workspace/response.txt")
    )
    parser.add_argument("--output-dir", type=Path, default=Path("/logs/verifier"))
    args = parser.parse_args()
    return verify(args.metadata, args.response, args.output_dir)


if __name__ == "__main__":
    raise SystemExit(main())
