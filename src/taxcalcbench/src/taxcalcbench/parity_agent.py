"""`taxcalcbench-direct`: Harbor port of TaxCalcBench's own generation call.

Upstream TaxCalcBench does not run an agent. For each case,
``tax_return_generator.generate_tax_return`` makes one OpenAI Responses API
call through LiteLLM and scores the reply text. This agent sends the same call
from inside Harbor, so the parity experiment compares the same generation on
both sides and only the harness differs.

What is matched (upstream source in parentheses):

- prompt: the upstream prompt for the case. ``instruction.md`` is that prompt
  followed by a "Harbor task notes" block; the agent drops the block, so the
  text sent is the upstream prompt byte for byte (unit-tested);
- TY25 input: one user message with an ``input_text`` part (the prompt) and one
  ``input_file`` part per PDF, base64 data URLs, PDFs sorted by file name
  (``build_ty25_response_input``). The PDFs are read from the task container's
  ``/app/input``;
- TY24 input: the prompt string (``generate_tax_return``);
- call arguments: ``model``, ``input``, ``reasoning.effort`` from the thinking
  level (``openai_reasoning_effort``); TY25 adds ``stream=True`` and
  ``timeout=TY25_LONG_RUN_TIMEOUT`` (14,400 s) (``generate_tax_return``);
- reply text: upstream's own stream collector / text extractor
  (``_stream_openai_response``, ``_extract_openai_response_text``), imported
  from the pinned checkout together with ``openai_reasoning_effort`` and the
  model registration helper;
- no retries (upstream makes one call). A failed call raises, so Harbor records
  an exception rather than a score of 0.

The reply is written to ``/app/output/return.txt``, which the task's verifier
scores. Point ``TAXCALCBENCH_ROOT`` at the upstream checkout pinned in
``main.py`` and install the ``parity`` extra (upstream's own dependency pins).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

HARBOR_NOTES_MARKER = "\n## Harbor task notes\n"
INPUT_DIR = "/app/input"
OUTPUT_PATH = "/app/output/return.txt"


def upstream_prompt_from_instruction(instruction: str) -> str:
    """Return the upstream prompt: the instruction without the Harbor notes."""
    if HARBOR_NOTES_MARKER not in instruction:
        raise ValueError("instruction.md has no Harbor task notes block")
    return instruction.rsplit(HARBOR_NOTES_MARKER, 1)[0]


def build_ty25_response_input(prompt: str, pdf_paths: list[Path]) -> list[dict[str, Any]]:
    """Same structure as upstream ``build_ty25_response_input``."""
    content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    for pdf_path in sorted(pdf_paths, key=lambda p: p.name):
        encoded_pdf = base64.b64encode(pdf_path.read_bytes()).decode("ascii")
        content.append(
            {
                "type": "input_file",
                "filename": pdf_path.name,
                "file_data": f"data:application/pdf;base64,{encoded_pdf}",
            }
        )
    return [{"role": "user", "content": content}]


def _load_upstream() -> dict[str, Any]:
    root = os.environ.get("TAXCALCBENCH_ROOT")
    if not root:
        raise RuntimeError("Set TAXCALCBENCH_ROOT to the pinned tax-calc-bench checkout")
    if root not in sys.path:
        sys.path.insert(0, root)
    import litellm  # noqa: F401  (upstream's pinned version via the parity extra)
    from litellm import responses
    from tax_calc_bench import tax_return_generator as gen
    from tax_calc_bench.config import openai_reasoning_effort

    return {"responses": responses, "gen": gen, "openai_reasoning_effort": openai_reasoning_effort}


class TaxCalcBenchDirectAgent(BaseAgent):
    def __init__(self, *args, thinking_level: str = "high", **kwargs):
        super().__init__(*args, **kwargs)
        self._thinking_level = thinking_level

    @staticmethod
    def name() -> str:
        return "taxcalcbench-direct"

    def version(self) -> str | None:
        return "1.0.0"

    async def setup(self, environment: BaseEnvironment) -> None:
        return

    def build_request(self, prompt: str, input_dir: Path) -> dict[str, Any]:
        """Build the LiteLLM ``responses`` arguments exactly as upstream does."""
        up = _load_upstream()
        if not self.model_name or "/" not in self.model_name:
            raise RuntimeError("model_name must look like 'openai/<model>'")
        provider, model_id = self.model_name.split("/", 1)
        if provider != "openai":
            raise RuntimeError("taxcalcbench-direct ports upstream's OpenAI path only")
        effort = up["openai_reasoning_effort"](model_id, self._thinking_level)
        if effort is None:
            raise RuntimeError(f"{model_id} does not support thinking level {self._thinking_level}")

        pdfs = sorted(input_dir.glob("*.pdf"))
        args: dict[str, Any] = {
            "model": self.model_name,
            "input": build_ty25_response_input(prompt, pdfs) if pdfs else prompt,
            "reasoning": {"effort": effort},
        }
        if pdfs:  # TY25 cases have PDF inputs; upstream streams every TY25 call.
            args["timeout"] = up["gen"].TY25_LONG_RUN_TIMEOUT
            args["stream"] = True
        elif effort == "xhigh":
            args["timeout"] = up["gen"].TY25_LONG_RUN_TIMEOUT
            args["stream"] = True
        return args

    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        up = _load_upstream()
        gen = up["gen"]
        model_id = self.model_name.split("/", 1)[1] if self.model_name else ""
        # Same model registration upstream does before the call.
        ensure = {
            "gpt-6.2-sol": "_ensure_openai_gpt62_sol_registered",
            "gpt-6.2-astra": "_ensure_openai_gpt62_astra_registered",
            "gpt-6.1-sol": "_ensure_openai_gpt61_sol_registered",
        }.get(model_id)
        if ensure:
            getattr(gen, ensure)()

        prompt = upstream_prompt_from_instruction(instruction)
        with tempfile.TemporaryDirectory() as tmp:
            input_dir = Path(tmp) / "input"
            await environment.download_dir(INPUT_DIR, input_dir)
            args = self.build_request(prompt, input_dir)

        def call() -> tuple[str, Any]:
            # Send the request and read the whole reply in one worker thread,
            # as upstream does in its single synchronous call path.
            response = up["responses"](**args)
            if args.get("stream"):
                text, _, accounting = gen._stream_openai_response(response)
                return text, accounting
            return gen._extract_openai_response_text(response), response

        text, accounting = await asyncio.to_thread(call)

        usage = getattr(accounting, "usage", None) if accounting is not None else None
        if usage is not None:
            context.n_input_tokens = getattr(usage, "input_tokens", None)
            context.n_output_tokens = getattr(usage, "output_tokens", None)

        self.logs_dir.mkdir(parents=True, exist_ok=True)
        (self.logs_dir / "return.txt").write_text(text)
        (self.logs_dir / "request_meta.json").write_text(
            json.dumps(
                {
                    "model": args["model"],
                    "reasoning": args["reasoning"],
                    "stream": args.get("stream", False),
                    "timeout": args.get("timeout"),
                    "n_pdfs": len(args["input"][0]["content"]) - 1 if isinstance(args["input"], list) else 0,
                    "thinking_level": self._thinking_level,
                },
                indent=2,
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "return.txt"
            out.write_text(text)
            await environment.exec("mkdir -p /app/output")
            await environment.upload_file(out, OUTPUT_PATH)
