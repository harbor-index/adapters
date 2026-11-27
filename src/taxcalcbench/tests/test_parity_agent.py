"""The parity agent builds the same request as upstream, for every case.

No API call is made. Run from src/taxcalcbench/ with the ``parity`` extra:
    TAXCALCBENCH_ROOT=/path/to/tax-calc-bench uv run --extra parity --with pytest pytest tests
"""

from __future__ import annotations

import asyncio
import codecs
import json
import locale
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = os.environ.get("TAXCALCBENCH_ROOT")
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")  # as upstream; read when LiteLLM is imported
_UTF8 = codecs.lookup(locale.getpreferredencoding(False)).name == "utf-8"
pytestmark = [pytest.mark.skipif(not ROOT, reason="set TAXCALCBENCH_ROOT to an upstream checkout"),
              pytest.mark.skipif(not _UTF8, reason="the parity agent runs only where text is read as UTF-8")]

MODEL = "openai/gpt-6-luna"


@pytest.fixture(scope="module")
def tasks(tmp_path_factory):
    from taxcalcbench.adapter import TaxCalcBenchAdapter

    out = tmp_path_factory.mktemp("tasks")
    adapter = TaxCalcBenchAdapter(output_dir=out, benchmark_root=Path(ROOT))
    adapter.run()
    return adapter, out


@pytest.fixture(scope="module")
def upstream():
    from taxcalcbench.parity_agent import _load_upstream

    up = _load_upstream()
    from tax_calc_bench.config import TAX_YEAR
    from tax_calc_bench.ty24_prompt import TAX_RETURN_GENERATION_PROMPT

    up["TY24"] = (TAX_YEAR, TAX_RETURN_GENERATION_PROMPT)
    return up


@pytest.fixture(scope="module")
def agent(tmp_path_factory):
    from taxcalcbench.parity_agent import TaxCalcBenchDirectAgent

    return TaxCalcBenchDirectAgent(logs_dir=tmp_path_factory.mktemp("logs"), model_name=MODEL)


def _upstream_request(gen, case, monkeypatch, model=MODEL) -> dict:
    """Run upstream's real per-test path and capture the request it would send."""
    captured: list[dict] = []

    class _Stop(BaseException):
        pass

    def fake_responses(**kwargs):
        captured.append(kwargs)
        raise _Stop()

    monkeypatch.setattr(gen, "responses", fake_responses)
    try:
        gen.run_tax_return_test(model, case.source_id, "high", None, case.tax_year)
    except _Stop:
        pass
    assert len(captured) == 1, case.task_id
    return captured[0]


def test_request_matches_upstream(tasks, upstream, agent, monkeypatch):
    """The agent's request equals the one upstream's own code path builds."""
    from taxcalcbench.parity_agent import upstream_prompt_from_instruction

    adapter, out = tasks
    gen = upstream["gen"]
    monkeypatch.chdir(ROOT)  # upstream reads test data relative to cwd

    cases = adapter.discover()
    assert sum(c.tax_year == "ty25" for c in cases) == 50
    assert sum(c.tax_year == "ty24" for c in cases) == 51
    for case in cases:
        task_dir = out / case.task_id
        prompt = upstream_prompt_from_instruction((task_dir / "instruction.md").read_text())
        mine = agent.build_request(prompt, task_dir / "environment" / "input")
        theirs = _upstream_request(gen, case, monkeypatch)
        assert mine == theirs, case.task_id


@pytest.mark.parametrize("model", ["openai/gpt-5.4", "openai/gpt-5-mini"])
def test_ty24_request_matches_upstream_for_other_openai_models(tasks, upstream, tmp_path, monkeypatch, model):
    """Unmodified upstream accepts any OpenAI model for TY2024, so the fallback
    models on the team API can be compared without a fork on that half."""
    from taxcalcbench.parity_agent import TaxCalcBenchDirectAgent, upstream_prompt_from_instruction

    adapter, out = tasks
    monkeypatch.chdir(ROOT)
    agent = TaxCalcBenchDirectAgent(logs_dir=tmp_path, model_name=model)
    cases = [c for c in adapter.discover() if c.tax_year == "ty24"]
    assert len(cases) == 51
    for case in cases:
        task_dir = out / case.task_id
        prompt = upstream_prompt_from_instruction((task_dir / "instruction.md").read_text())
        mine = agent.build_request(prompt, task_dir / "environment" / "input")
        assert mine == _upstream_request(upstream["gen"], case, monkeypatch, model), (model, case.task_id)


def test_unmodified_upstream_rejects_unlisted_models_for_ty25():
    # Why a fallback model needs a fork for the TY2025 half.
    import sys

    sys.path.insert(0, ROOT)
    try:
        from tax_calc_bench.config import validate_ty25_model_selection
    finally:
        sys.path.remove(ROOT)
    validate_ty25_model_selection("openai", "gpt-6-luna", None)
    with pytest.raises(ValueError):
        validate_ty25_model_selection("openai", "gpt-5.4", None)


def test_expected_commit_override(monkeypatch, tmp_path):
    from taxcalcbench.adapter import PINNED_REF, check_pinned_checkout, expected_ref

    assert expected_ref() == PINNED_REF and check_pinned_checkout(Path(ROOT)) == PINNED_REF
    monkeypatch.setenv("TAXCALCBENCH_EXPECTED_REF", "0" * 40)
    # The parity tooling follows the variable; task generation keeps the pin.
    with pytest.raises(RuntimeError, match="expected 0000"):
        check_pinned_checkout(Path(ROOT), expected_ref())
    assert check_pinned_checkout(Path(ROOT)) == PINNED_REF


def test_model_alias_is_sent_under_upstreams_canonical_name(tasks, upstream, tmp_path, monkeypatch):
    """Upstream's CLI turns an alias into the canonical model name before any call."""
    from taxcalcbench.parity_agent import TaxCalcBenchDirectAgent, upstream_prompt_from_instruction

    adapter, out = tasks
    monkeypatch.chdir(ROOT)
    canonical = "openai/" + upstream["canonicalize_model_name"]("openai", "gpt-5.6")
    assert canonical == "openai/gpt-5.6-sol"
    agent = TaxCalcBenchDirectAgent(logs_dir=tmp_path, model_name="openai/gpt-5.6")
    cases = adapter.discover()
    for case in [c for c in cases if c.tax_year == "ty25"][:5] + [c for c in cases if c.tax_year == "ty24"][:5]:
        task_dir = out / case.task_id
        prompt = upstream_prompt_from_instruction((task_dir / "instruction.md").read_text(encoding="utf-8"))
        mine = agent.build_request(prompt, task_dir / "environment" / "input")
        assert mine["model"] == canonical
        assert mine == _upstream_request(upstream["gen"], case, monkeypatch, canonical), case.task_id


def test_preflight_needs_upstreams_litellm_setting(monkeypatch):
    from taxcalcbench.parity_agent import TaxCalcBenchDirectAgent

    TaxCalcBenchDirectAgent.preflight({}, None)
    monkeypatch.delenv("LITELLM_LOCAL_MODEL_COST_MAP")
    with pytest.raises(RuntimeError, match="LITELLM_LOCAL_MODEL_COST_MAP"):
        TaxCalcBenchDirectAgent.preflight({}, None)


def test_parity_config_marks_replyless_trials_invalid():
    import yaml
    from harbor.models.job.config import JobConfig

    path = Path(__file__).resolve().parents[1] / "run_taxcalcbench_parity.yaml"
    config = JobConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    assert config.n_attempts == 1 and config.agents[0].n_concurrent == 1
    assert config.agents[0].override_timeout_sec > 14400  # above upstream's TY2025 request timeout
    for name in ("AgentTimeoutError", "RuntimeError"):
        policy = config.override_error_policy[name]
        assert policy.has_valid_reward is False and policy.should_retry is False


def test_prompt_needs_notes_block():
    from taxcalcbench.parity_agent import upstream_prompt_from_instruction

    with pytest.raises(ValueError):
        upstream_prompt_from_instruction("no notes here")


# ---- failure handling and evidence (no API call; the LiteLLM call is replaced) ----
class _FakeEnv:
    def __init__(self, input_dir: Path, upload_error: Exception | None = None):
        self.input_dir = input_dir
        self.upload_error = upload_error
        self.uploaded: str | None = None

    async def download_dir(self, source_dir, target_dir):
        shutil.copytree(self.input_dir, target_dir)

    async def exec(self, command, **kwargs):
        return None

    async def upload_file(self, source_path, target_path):
        if self.upload_error is not None:
            raise self.upload_error
        self.uploaded = Path(source_path).read_bytes().decode("utf-8")


def _patch_responses(monkeypatch, fake_responses):
    import taxcalcbench.parity_agent as pa

    real_load = pa._load_upstream

    def load():
        up = real_load()
        up["responses"] = fake_responses
        return up

    monkeypatch.setattr(pa, "_load_upstream", load)
    return pa


def _meta(logs: Path):
    meta_file = logs / "request_meta.json"
    return json.loads(meta_file.read_text()) if meta_file.exists() else None


def _plain_reply(text="Line 1 | x | 5"):
    """A non-streamed Responses API reply, as upstream's text extractor reads it."""
    return SimpleNamespace(output_text=text, id="resp_2", model="gpt-6-luna-test", status="completed",
                           usage=SimpleNamespace(input_tokens=3, output_tokens=4))


def _run_agent(tasks, tmp_path, monkeypatch, task_id, fake_responses, instruction=None, env=None):
    from harbor.models.agent.context import AgentContext

    _, out = tasks
    pa = _patch_responses(monkeypatch, fake_responses)
    task_dir = out / task_id
    agent = pa.TaxCalcBenchDirectAgent(logs_dir=tmp_path / "logs", model_name=MODEL)
    env = env or _FakeEnv(task_dir / "environment" / "input")
    instruction = (task_dir / "instruction.md").read_text() if instruction is None else instruction
    error = None
    try:
        asyncio.run(agent.run(instruction, env, AgentContext()))
    except Exception as exc:  # noqa: BLE001
        error = exc
    meta_file = tmp_path / "logs" / "request_meta.json"
    meta = json.loads(meta_file.read_text()) if meta_file.exists() else None
    return error, env, meta


def _events(text, completed=True):
    events = [{"type": "response.output_text.delta", "delta": text}]
    if completed:
        events.append({"type": "response.completed",
                       "response": {"id": "resp_1", "model": "gpt-6-luna-test", "status": "completed",
                                    "usage": {"input_tokens": 7, "output_tokens": 9}}})
    return events


def test_stream_failure_is_an_api_error_not_a_score(tasks, tmp_path, monkeypatch):
    from harbor.models.trial.error_policy import policy_for

    def failing(**kwargs):
        return iter([{"type": "response.failed", "response": {"error": {"message": "boom"}}}])

    error, env, meta = _run_agent(tasks, tmp_path, monkeypatch, "ty25-ca-004", failing)
    assert type(error).__name__ == "UnknownApiError"
    assert type(error.__cause__).__name__ == "GenerationStreamError"
    assert policy_for(type(error).__name__).has_valid_reward is False
    assert env.uploaded is None
    assert meta["outcome"] == "api_failure" and meta["error_type"] == "GenerationStreamError"


def test_known_valid_reward_errors_are_remapped(tasks, tmp_path, monkeypatch):
    # Harbor treats these as valid results (a wrong answer); upstream leaves a
    # failed generation unscored, so the agent re-raises them as invalid.
    from harbor.models.trial.error_policy import policy_for

    class ContextWindowExceededError(Exception):
        pass

    assert policy_for("ContextWindowExceededError").has_valid_reward is True

    def failing(**kwargs):
        raise ContextWindowExceededError("too long")

    error, env, meta = _run_agent(tasks, tmp_path, monkeypatch, "ty24-single-senior-blind-over-65", failing)
    assert type(error).__name__ == "UnknownApiError"
    assert type(error.__cause__).__name__ == "ContextWindowExceededError"
    assert env.uploaded is None and meta["outcome"] == "api_failure"


def test_known_litellm_error_keeps_its_type(tasks, tmp_path, monkeypatch):
    from harbor.models.trial.error_policy import policy_for

    class RateLimitError(Exception):
        pass

    def failing(**kwargs):
        raise RateLimitError("slow down")

    error, env, meta = _run_agent(tasks, tmp_path, monkeypatch, "ty24-single-senior-blind-over-65", failing)
    assert type(error).__name__ == "RateLimitError"
    assert policy_for("RateLimitError").has_valid_reward is False
    assert env.uploaded is None and meta["outcome"] == "api_failure"


def test_stream_reply_is_written_and_completion_recorded(tasks, tmp_path, monkeypatch):
    error, env, meta = _run_agent(tasks, tmp_path, monkeypatch, "ty25-ca-004", lambda **kw: iter(_events("Line 1 | x | 5")))
    assert error is None and env.uploaded == "Line 1 | x | 5"
    assert meta["outcome"] == "ok" and meta["stream_completed"] is True
    assert meta["served_model"] == "gpt-6-luna-test" and meta["input_tokens"] == 7
    # The versions that built and sent the request, for the comparison with the original side.
    from importlib.metadata import version

    assert meta["litellm_version"] == version("litellm") and meta["openai_version"] == version("openai")
    assert meta["harbor_version"] == version("harbor")


def test_stream_without_completion_event_is_flagged(tasks, tmp_path, monkeypatch):
    # Upstream's collector accepts such a reply, so the agent does too, but the
    # missing completion event is recorded for the parity report.
    error, env, meta = _run_agent(tasks, tmp_path, monkeypatch, "ty25-ca-004",
                                  lambda **kw: iter(_events("partial", completed=False)))
    assert error is None and env.uploaded == "partial"
    assert meta["stream_completed"] is False


def test_agent_side_failure_is_not_a_score_either(tasks, tmp_path, monkeypatch):
    # A failure before the model call (here: an instruction without the notes
    # block) must also leave run() as a class Harbor marks as an invalid reward.
    from harbor.models.trial.error_policy import policy_for

    def never_called(**kwargs):
        raise AssertionError("the model must not be called")

    error, env, meta = _run_agent(tasks, tmp_path, monkeypatch, "ty25-ca-004", never_called,
                                  instruction="some prompt without the block")
    assert type(error).__name__ == "UnknownApiError" and isinstance(error.__cause__, ValueError)
    assert policy_for(type(error).__name__).has_valid_reward is False
    assert policy_for("ValueError").has_valid_reward is True  # why the mapping is needed
    assert env.uploaded is None and meta is None


TY24_TASK = "ty24-single-senior-blind-over-65"


def test_ty24_reply_without_streaming_is_written(tasks, tmp_path, monkeypatch):
    seen = []

    def fake(**kwargs):
        seen.append(kwargs)
        return _plain_reply()

    error, env, meta = _run_agent(tasks, tmp_path, monkeypatch, TY24_TASK, fake)
    assert error is None and env.uploaded == "Line 1 | x | 5"
    assert "stream" not in seen[0] and "timeout" not in seen[0]  # as upstream at thinking level high
    assert meta["outcome"] == "ok" and meta["stream"] is False and meta["request_sent"] is True
    assert meta["response_status"] == "completed" and meta["served_model"] == "gpt-6-luna-test"
    assert (tmp_path / "logs" / "return.txt").read_text() == "Line 1 | x | 5"


@pytest.mark.parametrize("task_id, drop", [("ty25-ca-004", "one_pdf"), ("ty25-ca-004", "json"),
                                             ("ty25-ca-004", "all"), (TY24_TASK, "all")])
def test_incomplete_download_never_becomes_another_request(tasks, tmp_path, monkeypatch, task_id, drop):
    # The prompt lists the case's files; a download that lost some of them must
    # stop the trial, not send a smaller (or a TY2024-shaped) request.
    _, out = tasks
    partial = tmp_path / "partial"
    shutil.copytree(out / task_id / "environment" / "input", partial)
    doomed = {"one_pdf": sorted(partial.glob("*.pdf"))[:1], "json": [partial / "remaining_data.json"],
              "all": sorted(partial.iterdir())}[drop]
    for f in doomed:
        f.unlink()

    def never_called(**kwargs):
        raise AssertionError("the model must not be called")

    error, env, meta = _run_agent(tasks, tmp_path, monkeypatch, task_id, never_called, env=_FakeEnv(partial))
    assert type(error).__name__ == "UnknownApiError" and isinstance(error.__cause__, RuntimeError)
    assert env.uploaded is None and meta is None


def test_reply_that_cannot_be_delivered_is_recorded_as_such(tasks, tmp_path, monkeypatch):
    env = _FakeEnv(tasks[1] / TY24_TASK / "environment" / "input", upload_error=RuntimeError("cp failed"))
    error, env, meta = _run_agent(tasks, tmp_path, monkeypatch, TY24_TASK, lambda **kw: _plain_reply(), env=env)
    assert type(error).__name__ == "UnknownApiError" and "did not deliver" in str(error)
    assert meta["outcome"] == "reply_not_delivered" and meta["request_sent"] is True
    assert (tmp_path / "logs" / "return.txt").read_text() == "Line 1 | x | 5"  # the reply is kept on the host


def test_trial_cancelled_while_waiting_sends_no_request(tasks, tmp_path, monkeypatch):
    from harbor.models.agent.context import AgentContext

    calls, release = [], threading.Event()

    def fake(**kwargs):
        calls.append(1)
        release.wait(10)
        return _plain_reply()

    pa = _patch_responses(monkeypatch, fake)
    task_dir = tasks[1] / TY24_TASK
    instruction = (task_dir / "instruction.md").read_text(encoding="utf-8")
    first = pa.TaxCalcBenchDirectAgent(logs_dir=tmp_path / "first", model_name=MODEL)
    second = pa.TaxCalcBenchDirectAgent(logs_dir=tmp_path / "second", model_name=MODEL)
    env_1, env_2 = (_FakeEnv(task_dir / "environment" / "input") for _ in range(2))

    async def scenario():
        running = asyncio.create_task(first.run(instruction, env_1, AgentContext()))
        deadline = time.time() + 10
        while not calls:
            assert time.time() < deadline, "the first trial never reached the model call"
            await asyncio.sleep(0.01)
        waiting = asyncio.create_task(second.run(instruction, env_2, AgentContext()))
        await asyncio.sleep(0.3)  # the second trial is now waiting for its turn
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        release.set()
        await running

    asyncio.run(scenario())
    assert len(calls) == 1  # the cancelled trial never reached the API
    assert env_1.uploaded == "Line 1 | x | 5" and env_2.uploaded is None
    meta = _meta(tmp_path / "second")
    assert meta["outcome"] == "cancelled" and meta["request_sent"] is False
    assert _meta(tmp_path / "first")["outcome"] == "ok"


def test_abandoned_trial_sends_nothing_even_without_the_gate(tasks, tmp_path, monkeypatch):
    # Second line of defence, for trials that do not share an event loop (so
    # no shared gate): a worker thread that gets its turn after its trial was
    # cancelled must not send the request.
    from harbor.models.agent.context import AgentContext

    calls, release = [], threading.Event()

    def fake(**kwargs):
        calls.append(1)
        release.wait(10)
        return _plain_reply()

    pa = _patch_responses(monkeypatch, fake)
    monkeypatch.setattr(pa, "_call_gate", lambda: asyncio.Lock())  # a gate nobody shares
    task_dir = tasks[1] / TY24_TASK
    instruction = (task_dir / "instruction.md").read_text(encoding="utf-8")
    first = pa.TaxCalcBenchDirectAgent(logs_dir=tmp_path / "first", model_name=MODEL)
    second = pa.TaxCalcBenchDirectAgent(logs_dir=tmp_path / "second", model_name=MODEL)
    env_1, env_2 = (_FakeEnv(task_dir / "environment" / "input") for _ in range(2))

    async def scenario():
        running = asyncio.create_task(first.run(instruction, env_1, AgentContext()))
        deadline = time.time() + 10
        while not calls:
            assert time.time() < deadline, "the first trial never reached the model call"
            await asyncio.sleep(0.01)
        waiting = asyncio.create_task(second.run(instruction, env_2, AgentContext()))
        await asyncio.sleep(0.3)  # its worker thread is now waiting for the wire
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        release.set()
        await running
        await asyncio.sleep(0.3)  # let the abandoned worker thread take its turn

    asyncio.run(scenario())
    assert len(calls) == 1 and env_2.uploaded is None
    assert _meta(tmp_path / "second")["outcome"] == "cancelled"


def test_trial_cut_off_mid_stream_stops_and_the_next_request_waits_for_it(tasks, tmp_path, monkeypatch):
    from harbor.models.agent.context import AgentContext

    progress = {"calls": 0, "first_events": 0, "at_second_call": None}

    def fake(**kwargs):
        progress["calls"] += 1
        if progress["calls"] == 1:
            def slow_stream():
                for _ in range(100):
                    progress["first_events"] += 1
                    yield {"type": "response.output_text.delta", "delta": "x"}
                    time.sleep(0.05)
            return slow_stream()
        progress["at_second_call"] = progress["first_events"]
        return iter(_events("Line 1 | x | 5"))

    pa = _patch_responses(monkeypatch, fake)
    task_dir = tasks[1] / "ty25-ca-004"
    instruction = (task_dir / "instruction.md").read_text(encoding="utf-8")
    first = pa.TaxCalcBenchDirectAgent(logs_dir=tmp_path / "first", model_name=MODEL)
    second = pa.TaxCalcBenchDirectAgent(logs_dir=tmp_path / "second", model_name=MODEL)
    env_1, env_2 = (_FakeEnv(task_dir / "environment" / "input") for _ in range(2))

    async def scenario():
        with pytest.raises(TimeoutError):  # what Harbor's trial time limit does to agent.run()
            await asyncio.wait_for(first.run(instruction, env_1, AgentContext()), timeout=0.4)
        await second.run(instruction, env_2, AgentContext())

    asyncio.run(scenario())
    meta = _meta(tmp_path / "first")
    assert meta["outcome"] == "cancelled" and meta["request_sent"] is True
    assert meta["stream_last_event"] == "response.output_text.delta" and meta["stream_events"] >= 1
    assert env_1.uploaded is None and not (tmp_path / "first" / "return.txt").exists()
    assert progress["first_events"] < 100  # the abandoned stream was stopped, not read to the end
    assert progress["at_second_call"] == progress["first_events"]  # and had stopped before the next request
    assert env_2.uploaded == "Line 1 | x | 5" and _meta(tmp_path / "second")["outcome"] == "ok"


def test_waiting_trials_work_in_a_second_event_loop(tasks, tmp_path, monkeypatch):
    # The gate belongs to the event loop that is running. A single gate for the
    # whole process would fail here: once trials have waited on it in one loop,
    # it cannot be awaited from another.
    from harbor.models.agent.context import AgentContext

    def fake(**kwargs):
        time.sleep(0.2)
        return _plain_reply()

    pa = _patch_responses(monkeypatch, fake)
    task_dir = tasks[1] / TY24_TASK
    instruction = (task_dir / "instruction.md").read_text(encoding="utf-8")

    async def two_trials(round_dir: Path):
        envs = [_FakeEnv(task_dir / "environment" / "input") for _ in range(2)]
        agents = [pa.TaxCalcBenchDirectAgent(logs_dir=round_dir / str(i), model_name=MODEL) for i in range(2)]
        await asyncio.gather(*(a.run(instruction, e, AgentContext()) for a, e in zip(agents, envs)))
        return [e.uploaded for e in envs]

    for round_name in ("first-loop", "second-loop"):
        assert asyncio.run(two_trials(tmp_path / round_name)) == ["Line 1 | x | 5"] * 2


def test_upstream_modules_must_come_from_the_checked_checkout(monkeypatch, tmp_path):
    # The request record names the checkout's commit, so code imported earlier
    # from some other copy of upstream must stop the agent.
    import taxcalcbench.parity_agent as pa

    up = pa._load_upstream()
    elsewhere = tmp_path / "other" / "tax_calc_bench" / "tax_return_generator.py"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text("")
    monkeypatch.setattr(up["gen"], "__file__", str(elsewhere))
    with pytest.raises(RuntimeError, match="not from TAXCALCBENCH_ROOT"):
        pa._load_upstream()


def test_agent_refuses_a_non_utf8_process(monkeypatch):
    # Harbor reads instruction.md in the process's text encoding; anything but
    # UTF-8 would change the TY2025 prompts (they hold non-ASCII characters).
    import locale

    import taxcalcbench.parity_agent as pa

    monkeypatch.setattr(locale, "getpreferredencoding", lambda do_setlocale=True: "ISO8859-1")
    with pytest.raises(RuntimeError, match="instruction.md as ISO8859-1"):
        pa._load_upstream()
    monkeypatch.setattr(locale, "getpreferredencoding", lambda do_setlocale=True: "utf8")  # another name of UTF-8
    pa._load_upstream()


def test_endpoint_is_recorded_with_port_and_path_and_without_credentials(tasks, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://user:secret@Gateway.Example:4000/team-b/v1/")
    error, env, meta = _run_agent(tasks, tmp_path, monkeypatch, TY24_TASK, lambda **kw: _plain_reply())
    assert error is None and meta["api_base"] == "http://gateway.example:4000/team-b/v1"
    assert "secret" not in json.dumps(meta)


def test_reply_that_cannot_be_written_is_not_recorded_as_delivered(tasks, tmp_path, monkeypatch):
    # A reply with half a surrogate pair cannot be encoded: nothing is saved or uploaded.
    error, env, meta = _run_agent(tasks, tmp_path, monkeypatch, TY24_TASK, lambda **kw: _plain_reply("Line 1 | x | 5 \ud83d"))
    assert type(error).__name__ == "UnknownApiError" and isinstance(error.__cause__, UnicodeEncodeError)
    assert meta["outcome"] == "reply_not_delivered" and env.uploaded is None


def test_cancellation_record_agrees_with_what_went_out(tasks, tmp_path, monkeypatch):
    # The worker has just seen "not abandoned" and is about to send when the trial is cancelled.
    # The record must say what happens on the wire: the request goes out, so request_sent is true.
    from harbor.models.agent.context import AgentContext

    reached, gate, calls, made = threading.Event(), threading.Event(), [], []

    class PausingEvent(threading.Event):
        def is_set(self):
            value = super().is_set()
            if made and made[0] is self and threading.current_thread() is not threading.main_thread() and not gate.is_set():
                reached.set()
                gate.wait(5)
            return value

        def __init__(self):
            super().__init__()
            if sys._getframe(1).f_code.co_name == "_run":
                made.append(self)  # the first flag the agent makes for a run is "abandoned"

    def fake(**kwargs):
        calls.append(1)
        return _plain_reply()

    pa = _patch_responses(monkeypatch, fake)
    monkeypatch.setattr(pa.threading, "Event", PausingEvent)
    task_dir = tasks[1] / TY24_TASK
    agent = pa.TaxCalcBenchDirectAgent(logs_dir=tmp_path / "logs", model_name=MODEL)
    env = _FakeEnv(task_dir / "environment" / "input")

    async def scenario():
        running = asyncio.create_task(agent.run((task_dir / "instruction.md").read_text(encoding="utf-8"), env, AgentContext()))
        assert await asyncio.to_thread(reached.wait, 10)
        threading.Timer(0.3, gate.set).start()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        await asyncio.sleep(0.5)

    asyncio.run(scenario())
    meta = _meta(tmp_path / "logs")
    assert meta["outcome"] == "cancelled" and meta["request_sent"] is (len(calls) == 1)
