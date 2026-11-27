"""`taxcalcbench-direct`: Harbor port of TaxCalcBench's own generation call.

Upstream TaxCalcBench does not run an agent. For each case,
``tax_return_generator.generate_tax_return`` makes one OpenAI Responses API
call through LiteLLM and scores the reply text. This agent sends the same call
from inside Harbor, so the parity experiment compares the same generation on
both sides.

What is matched (upstream source in parentheses):

- prompt: the upstream prompt for the case. ``instruction.md`` is that prompt
  followed by an "Input files and answer location" block; the agent drops the block, so the
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
- no retries (upstream makes one call), and one call in flight at a time
  (upstream's runner is sequential): the parity config sets
  ``agents[].n_concurrent: 1``.

Failures. Upstream records a failed generation as ``generation_failed`` and
does not score it. Harbor decides from the exception's class name whether a
failed trial still has a valid reward, and treats names it does not know as a
valid result (a wrong answer); a few known names (context window exceeded,
content policy) are valid results too. Upstream leaves all of them unscored.
So every failure of this agent leaves ``run`` as a class whose reward Harbor
marks invalid: exception types that already have that policy (LiteLLM's rate
limit, connection and server errors) pass through, and anything else
(upstream's ``GenerationStreamError``, a context-window or content-policy
error, a missing input, a malformed instruction) is re-raised as Harbor's
``UnknownApiError`` with the original error chained. The one failure the agent
cannot re-raise is Harbor's own trial timeout; the parity config marks its
reward invalid (``override_error_policy``).

Evidence. For every trial that reaches the model call, ``request_meta.json``
in the agent log records the outcome (``ok``, ``api_failure``,
``reply_not_delivered``, ``cancelled`` for a call that was cut off, or
``started`` when the process ended before any of these could be written), the
request settings, the API base URL (without credentials), the upstream commit
and the LiteLLM, OpenAI SDK and Harbor versions. A finished, failed or cancelled call also records
whether the request had gone out (``request_sent``) and how far a stream got;
a completed call records the model name the API reported, the response id and
status, and whether the stream ended with ``response.completed``.

The reply is written to ``/app/output/return.txt``, which the task's verifier
scores. Point ``TAXCALCBENCH_ROOT`` at an unmodified checkout of the pinned
upstream commit (checked before any trial starts) and install the ``parity``
extra (LiteLLM and OpenAI SDK at upstream's locked versions).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import sys
import tempfile
import threading
import weakref
from pathlib import Path
from typing import Any

from harbor.agents.base import BaseAgent
from harbor.agents.installed.base import UnknownApiError
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.trial.error_policy import policy_for

from .adapter import NOTES_HEADING, check_pinned_checkout, expected_ref

NOTES_MARKER = f"\n{NOTES_HEADING}\n"
INPUT_DIR = "/app/input"
OUTPUT_PATH = "/app/output/return.txt"


def upstream_prompt_from_instruction(instruction: str) -> str:
    """Return the upstream prompt: the instruction without the appended notes."""
    if NOTES_MARKER not in instruction:
        raise ValueError(f"instruction.md has no {NOTES_HEADING!r} block")
    return instruction.rsplit(NOTES_MARKER, 1)[0]


_PDF_LIST = re.compile(r"\nAttached PDF files:\n((?:- [^\n]+\n)+)\nremaining_data\.json:\n")


def expected_pdf_names(prompt: str) -> list[str] | None:
    """PDF names a TY2025 prompt lists (sorted, as upstream attaches them); None for a TY2024 prompt."""
    match = _PDF_LIST.search(prompt)
    if match is None:
        if "Attached PDF files:" in prompt:
            raise ValueError("TY2025 prompt with an unreadable PDF list")
        return None
    return sorted(line[2:] for line in match.group(1).splitlines())


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


_CHECKED_ROOTS: dict[str, str] = {}
# Upstream's runner makes its model calls one at a time, and concurrent
# streamed calls through LiteLLM's synchronous client in worker threads failed
# now and then with "Bad file descriptor". The parity config therefore runs one
# agent at a time (agents[].n_concurrent: 1), which Harbor applies before the
# trial's time limit starts. The two locks below keep that property when the
# agent is started some other way:
# - the call gate (one per event loop): trials wait for their turn in the event
#   loop, so a trial that is cancelled while waiting never sends a request;
# - _WIRE_LOCK: no two requests are on the wire at once, even if a cancelled
#   trial left a worker thread behind. A streamed call that was cut off stops at
#   its next stream event; a non-streamed one runs until the API answers, and
#   the next trial waits for it inside its own time limit.
_CALL_GATES: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = weakref.WeakKeyDictionary()
_WIRE_LOCK = threading.Lock()


def _call_gate() -> asyncio.Lock:
    """The gate of the running event loop (one per loop, so a second loop in the same process works)."""
    loop = asyncio.get_running_loop()
    gate = _CALL_GATES.get(loop)
    if gate is None:
        gate = _CALL_GATES[loop] = asyncio.Lock()
    return gate


class _StreamTap:
    """Pass-through wrapper that notes whether the stream reached ``response.completed``.

    Upstream's stream collector iterates the response and, on errors, reads
    attributes of it; both are forwarded unchanged.
    """

    def __init__(self, inner: Any, abandoned: threading.Event | None = None,
                 info: dict[str, Any] | None = None) -> None:
        self._inner = inner
        self._abandoned = abandoned
        # Progress is written here event by event, so a call that is cut off
        # still shows how far its stream got.
        self._info = info if info is not None else {}
        self._info.update(last_event=None, completed=False, events=0)

    def __iter__(self):
        for event in self._inner:
            if self._abandoned is not None and self._abandoned.is_set():
                # The trial is over: stop reading, so the next trial's request can go out.
                raise RuntimeError("the trial ended while the reply was streaming")
            event_type = event.get("type") if isinstance(event, dict) else getattr(event, "type", None)
            event_type = getattr(event_type, "value", event_type)
            self._info["events"] += 1
            if event_type:
                self._info["last_event"] = str(event_type)
                if self._info["last_event"] == "response.completed":
                    self._info["completed"] = True
            yield event

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _api_base() -> str:
    """The endpoint LiteLLM's OpenAI provider will call: scheme, host, port and path, no credentials."""
    from urllib.parse import urlsplit

    base = os.environ.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_API_BASE") or "https://api.openai.com/v1"
    parts = urlsplit(base)
    if not parts.scheme or not parts.hostname:
        return ""
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname}{port}{parts.path.rstrip('/')}"


def _versions() -> dict[str, str | None]:
    """Versions of the packages that build and send the request (compared with the original side)."""
    from importlib import metadata

    found: dict[str, str | None] = {}
    for key, package in (("litellm_version", "litellm"), ("openai_version", "openai"), ("harbor_version", "harbor")):
        try:
            found[key] = metadata.version(package)
        except metadata.PackageNotFoundError:
            found[key] = None
    return found


def _field(value: Any, key: str) -> Any:
    if value is None:
        return None
    return value.get(key) if isinstance(value, dict) else getattr(value, key, None)


def _load_upstream() -> dict[str, Any]:
    root = os.environ.get("TAXCALCBENCH_ROOT")
    if not root:
        raise RuntimeError("Set TAXCALCBENCH_ROOT to the pinned tax-calc-bench checkout")
    # Upstream sets this before importing LiteLLM (tax_calc_bench/__init__.py),
    # so LiteLLM uses the model table bundled with the pinned release. Harbor
    # imports LiteLLM first, so the variable has to be set when Harbor starts.
    if os.environ.get("LITELLM_LOCAL_MODEL_COST_MAP") != "True":
        raise RuntimeError(
            "Start Harbor with LITELLM_LOCAL_MODEL_COST_MAP=True so LiteLLM uses the same "
            "bundled model table as upstream"
        )
    # Harbor reads instruction.md in the process's text encoding, and upstream
    # reads its test data the same way on the original side. Both must be UTF-8
    # for the two sides to send the same prompt.
    import codecs
    import locale

    encoding = locale.getpreferredencoding(False)
    if codecs.lookup(encoding).name != "utf-8":
        raise RuntimeError(f"Harbor would read instruction.md as {encoding}; use a UTF-8 locale or set PYTHONUTF8=1")
    if root not in _CHECKED_ROOTS:
        if _CHECKED_ROOTS:
            # Python keeps the first checkout's modules; a second one would be ignored.
            raise RuntimeError(f"upstream was already loaded from {next(iter(_CHECKED_ROOTS))}; "
                               "use one TAXCALCBENCH_ROOT per process")
        # Prompts, scoring fields and the stream collector come from this
        # checkout, so it must be the pinned commit with no local changes.
        _CHECKED_ROOTS[root] = check_pinned_checkout(Path(root), expected_ref())
    if root not in sys.path:
        sys.path.insert(0, root)
    import litellm  # noqa: F401  (upstream's pinned version via the parity extra)
    from litellm import responses
    import tax_calc_bench.config as config
    from tax_calc_bench import tax_return_generator as gen
    from tax_calc_bench.config import (
        canonicalize_model_name,
        canonicalize_thinking_level,
        openai_reasoning_effort,
        validate_ty25_model_selection,
    )

    # The commit recorded with every request describes this checkout, so the
    # modules in use must be its files (and not an earlier import from elsewhere).
    for module in (gen, config):
        if not Path(module.__file__).resolve().is_relative_to(Path(root).resolve()):
            raise RuntimeError(f"{module.__name__} is loaded from {module.__file__}, not from TAXCALCBENCH_ROOT ({root})")
    return {"responses": responses, "gen": gen, "openai_reasoning_effort": openai_reasoning_effort,
            "canonicalize_model_name": canonicalize_model_name,
            "canonicalize_thinking_level": canonicalize_thinking_level,
            "validate_ty25_model_selection": validate_ty25_model_selection,
            "commit": _CHECKED_ROOTS[root]}


class TaxCalcBenchDirectAgent(BaseAgent):
    def __init__(self, *args, thinking_level: str = "high", **kwargs):
        super().__init__(*args, **kwargs)
        self._thinking_level = thinking_level

    @staticmethod
    def name() -> str:
        return "taxcalcbench-direct"

    def version(self) -> str | None:
        return "1.0.0"

    @classmethod
    def preflight(cls, kwargs: dict[str, Any] | None = None, env: Any = None) -> None:
        """Fail before any trial is queued if the upstream checkout is not usable."""
        super().preflight(kwargs, env)
        _load_upstream()

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
        # Upstream's CLI turns a model alias into the canonical name first (main.py).
        model_id = up["canonicalize_model_name"](provider, model_id)

        # The prompt says which case this is and which files belong to it; the
        # downloaded folder must match, so a partial download can never turn
        # into a different request.
        expected_pdfs = expected_pdf_names(prompt)
        pdfs = sorted(input_dir.glob("*.pdf"))
        if expected_pdfs is None:  # TY2024: the data is inside the prompt
            if pdfs or not (input_dir / "input.json").is_file():
                raise RuntimeError("TY2024 task, but /app/input does not hold exactly input.json")
        else:
            if [p.name for p in pdfs] != expected_pdfs or not (input_dir / "remaining_data.json").is_file():
                raise RuntimeError(f"TY2025 task, but the downloaded inputs differ from the prompt's file list "
                                   f"(expected {expected_pdfs}, found {[p.name for p in pdfs]})")
            # Upstream's TY2025 path checks the model against its own list.
            up["validate_ty25_model_selection"](provider, model_id, None)
        model_name = f"{provider}/{model_id}"
        effort = up["openai_reasoning_effort"](model_id, self._thinking_level)
        if effort is None:
            raise RuntimeError(f"{model_id} does not support thinking level {self._thinking_level}")

        args: dict[str, Any] = {
            "model": model_name,
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
        try:
            await self._run(instruction, environment, context)
        except Exception as exc:
            if not policy_for(type(exc).__name__).has_valid_reward:
                raise  # Harbor already marks this failure's reward invalid
            raise UnknownApiError(
                f"taxcalcbench-direct did not deliver a reply ({type(exc).__name__}: {exc})"
            ) from exc

    async def _run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        up = _load_upstream()
        gen = up["gen"]
        prompt = upstream_prompt_from_instruction(instruction)
        with tempfile.TemporaryDirectory() as tmp:
            input_dir = Path(tmp) / "input"
            await environment.download_dir(INPUT_DIR, input_dir)
            args = self.build_request(prompt, input_dir)

        # Same model registration upstream does before the call
        # (generate_tax_return, OpenAI branch).
        ensure = {
            "gpt-6-astra": "_ensure_openai_gpt6_astra_registered",
            "gpt-6-sol": "_ensure_openai_gpt6_sol_registered",
            "gpt-6-luna": "_ensure_openai_gpt6_luna_registered",
            "gpt-6.1-sol": "_ensure_openai_gpt61_sol_registered",
            "gpt-6.1-terra": "_ensure_openai_gpt61_terra_registered",
            "gpt-6.2-sol": "_ensure_openai_gpt62_sol_registered",
            "gpt-6.2-astra": "_ensure_openai_gpt62_astra_registered",
        }.get(args["model"].split("/", 1)[1])
        if ensure:
            getattr(gen, ensure)()

        stream_info: dict[str, Any] = {}
        abandoned = threading.Event()
        sent = threading.Event()
        state = threading.Lock()  # "abandoned" and "sent" change together, so the record cannot disagree with the wire

        def call() -> tuple[str, Any]:
            # Send the request and read the whole reply in one worker thread,
            # one call at a time, as upstream does in its synchronous loop.
            with _WIRE_LOCK:
                with state:
                    if abandoned.is_set():
                        raise RuntimeError("the trial ended before the request was sent")
                    sent.set()
                response = up["responses"](**args)
                if args.get("stream"):
                    text, _, accounting = gen._stream_openai_response(_StreamTap(response, abandoned, stream_info))
                    return text, accounting
                return gen._extract_openai_response_text(response), response

        self.logs_dir.mkdir(parents=True, exist_ok=True)
        meta: dict[str, Any] = {
            "agent": f"{self.name()}@{self.version()}",
            "model": args["model"],
            "reasoning": args["reasoning"],
            "stream": args.get("stream", False),
            "timeout": args.get("timeout"),
            "n_pdfs": len(args["input"][0]["content"]) - 1 if isinstance(args["input"], list) else 0,
            "thinking_level": up["canonicalize_thinking_level"](self._thinking_level),
            "upstream_commit": up["commit"],
            "api_base": _api_base(),
            **_versions(),
        }

        def write_meta() -> None:
            (self.logs_dir / "request_meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

        meta["outcome"] = "started"
        write_meta()  # so a trial that is cut off still leaves its request settings
        try:
            async with _call_gate():
                text, accounting = await asyncio.to_thread(call)
        except asyncio.CancelledError:
            with state:
                abandoned.set()
                was_sent = sent.is_set()
            # A request that is already on the wire cannot be recalled; its
            # reply, if any, is discarded.
            meta.update(outcome="cancelled", request_sent=was_sent,
                        stream_last_event=stream_info.get("last_event"),
                        stream_events=stream_info.get("events"))
            write_meta()
            raise
        except Exception as exc:
            meta.update(outcome="api_failure", request_sent=sent.is_set(),
                        error_type=type(exc).__name__, error=str(exc)[:500],
                        stream_last_event=stream_info.get("last_event"),
                        stream_completed=stream_info.get("completed"),
                        stream_events=stream_info.get("events"))
            write_meta()
            raise

        usage = _field(accounting, "usage")
        context.n_input_tokens = _field(usage, "input_tokens")
        context.n_output_tokens = _field(usage, "output_tokens")
        meta.update(
            outcome="ok",
            request_sent=True,
            served_model=_field(accounting, "model"),
            response_id=_field(accounting, "id"),
            response_status=_field(accounting, "status"),
            stream_last_event=stream_info.get("last_event"),
            stream_completed=stream_info.get("completed"),
            input_tokens=context.n_input_tokens,
            output_tokens=context.n_output_tokens,
            reply_chars=len(text),
        )
        write_meta()

        try:
            # Bytes, so the reply reaches the scorer exactly as the model wrote it.
            (self.logs_dir / "return.txt").write_bytes(text.encode("utf-8"))
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "return.txt"
                out.write_bytes(text.encode("utf-8"))
                await environment.exec("mkdir -p /app/output")
                await environment.upload_file(out, OUTPUT_PATH)
        except BaseException:
            # The model replied, but the reply did not reach the task container.
            meta["outcome"] = "reply_not_delivered"
            write_meta()
            raise
