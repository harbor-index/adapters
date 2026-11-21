"""Score one response with the unmodified upstream PhySciBench judge."""

import asyncio
import json
import math
from pathlib import Path

from eval.config import ScorerConfig
from eval.data import load_benchmark
from eval.judge import PhySciBenchJudge

WORKSPACE = Path("/workspace")
LOGS = Path("/logs/verifier")


async def main() -> None:
    (sample,) = load_benchmark(Path(__file__).with_name("sample.json"))
    answer = WORKSPACE / "response.txt"
    if answer.is_file() and not answer.is_symlink():
        try:
            sample.response = answer.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            sample.response = None

    config = ScorerConfig.from_env(data_dir=WORKSPACE)
    sample = await PhySciBenchJudge(config).judge_one(sample)
    verdict = sample.judged_response or ""
    LOGS.mkdir(parents=True, exist_ok=True)
    (LOGS / "result.json").write_text(
        json.dumps({
            "id": sample.id,
            "score": sample.score,
            "correct": sample.correct,
            "judged_response": sample.judged_response,
            "reasoning": sample.reasoning,
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if verdict == "invalid" or verdict.startswith(("Evaluation error:", "Error during evaluation:")):
        raise RuntimeError("The upstream judge could not score this response. See verifier stderr.")
    score = sample.score
    if score is None or not math.isfinite(score) or not 0.0 <= score <= 1.0:
        raise ValueError("The upstream judge did not return a valid score.")
    (LOGS / "reward.txt").write_text(f"{score}\n", encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
