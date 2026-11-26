#!/usr/bin/env python3
"""Reproducible original-benchmark parity runner for HAEnv M2 Diagnosis.

Runs upstream `haenv` directly (`haenv.interaction.query.gated.solve_gated_round` +
`haenv.app.evaluate._base_row_gated` + `m2_raw_correct.raw_correct` +
`management-rubric-v7`) on the exact same `make_pack` batch and deterministic
stratified parity subset (`N=30`) used by the Harbor adapter.

Usage (inside upstream haenv environment):
  uv run --directory ~/Documents/haenv-upstream-94761fb python \\
    ~/Documents/adapters/src/haenv/run_original_parity.py \\
    --pack-dir ~/Documents/haenv-m2-pack-100 \\
    --split parity \\
    --model deepseek-ai/deepseek-v4.1-flash-1017 \\
    --output-dir ~/Documents/adapters/jobs/original-parity-trial1 \\
    --workers 4
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import importlib.util
import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def _load_adapter_parity_selector():
    adapter_py = Path(__file__).resolve().parent / "src" / "haenv" / "adapter.py"
    spec = importlib.util.spec_from_file_location("_haenv_harbor_adapter", adapter_py)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.PARITY_SUBSET_SIZE, mod.select_parity_subset


PARITY_SUBSET_SIZE, select_parity_subset = _load_adapter_parity_selector()


def _find_batch_dir(pack_dir: Path) -> Path:
    if (pack_dir / "cases.jsonl").is_file():
        return pack_dir
    manifest = pack_dir / "make_pack.json"
    if manifest.is_file():
        meta = json.loads(manifest.read_text(encoding="utf-8"))
        pack_batch = meta.get("pack_batch")
        if pack_batch:
            p = Path(pack_batch)
            if not p.is_absolute():
                p = pack_dir / p
            if (p / "cases.jsonl").is_file():
                return p
    candidates = sorted((pack_dir / "pack" / "results").glob("*/*/*/cases.jsonl"))
    if not candidates:
        candidates = sorted(pack_dir.rglob("cases.jsonl"))
    if not candidates:
        raise FileNotFoundError(f"Could not find cases.jsonl under {pack_dir}")
    return candidates[-1].parent


def _resolve_solver_endpoint(model: str, base_url_override: str = "") -> tuple[str, str, str]:
    base_url = (
        base_url_override
        or os.environ.get("HAENV_PARITY_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or ""
    ).strip()

    if os.environ.get("OPENROUTER_API_KEY") and (not base_url or "openrouter.ai" in base_url):
        url = (base_url or "https://openrouter.ai/api/v1/chat/completions").rstrip("/")
        if not url.endswith("/chat/completions"):
            url += "/chat/completions"
        return url, os.environ["OPENROUTER_API_KEY"], model

    if os.environ.get("OPENAI_API_KEY"):
        url = (base_url or "https://api.openai.com/v1").rstrip("/")
        if not url.endswith("/chat/completions"):
            url += "/chat/completions"
        if model.startswith("openai/"):
            model = model.removeprefix("openai/")
        return url, os.environ["OPENAI_API_KEY"], model

    if os.environ.get("GOOGLE_API_KEY"):
        url = (base_url or "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions").rstrip("/")
        if not url.endswith("/chat/completions"):
            url += "/chat/completions"
        return url, os.environ["GOOGLE_API_KEY"], model

    raise RuntimeError("Missing API key (OPENROUTER_API_KEY / OPENAI_API_KEY / GOOGLE_API_KEY)")


def _post_json_with_retry(url: str, api_key: str, body: dict[str, Any], timeout: int = 300) -> dict[str, Any]:
    payload_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
    last_exc: Exception | None = None
    for attempt in range(1, 5):
        req = urllib.request.Request(
            url,
            data=payload_bytes,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, dict) and data.get("error") and not data.get("choices"):
                raise RuntimeError(f"API error body: {str(data['error'])[:300]}")
            return data
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")[:300]
            last_exc = RuntimeError(f"HTTP {exc.code}: {err_body}")
            if exc.code not in (408, 429, 500, 502, 503, 504) or attempt == 4:
                raise last_exc from exc
            time.sleep(min(2.0 * attempt, 10.0))
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt == 4:
                raise
            time.sleep(min(2.0 * attempt, 10.0))
    raise last_exc or RuntimeError("Request failed")


def _call_live_judge(req: Any, policy: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    model = os.environ.get("HAENV_JUDGE_MODEL") or policy["judge"]["model_id"]
    base_url = os.environ.get("HAENV_JUDGE_BASE_URL", "").strip()
    api_key = ""

    if os.environ.get("OPENROUTER_API_KEY") and (not base_url or "openrouter.ai" in base_url):
        api_key = os.environ["OPENROUTER_API_KEY"]
        base_url = base_url or "https://openrouter.ai/api/v1/chat/completions"
    elif os.environ.get("OPENAI_API_KEY"):
        api_key = os.environ["OPENAI_API_KEY"]
        base_url = base_url or "https://api.openai.com/v1/chat/completions"
        if model.startswith("openai/"):
            model = model.removeprefix("openai/")
    elif os.environ.get("GOOGLE_API_KEY"):
        api_key = os.environ["GOOGLE_API_KEY"]
        base_url = base_url or "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    else:
        raise RuntimeError("No judge API key supplied")

    if not base_url.endswith("/chat/completions"):
        base_url = base_url.rstrip("/") + "/chat/completions"

    import copy

    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": req.prompt}],
        "max_tokens": int(policy["judge"].get("max_tokens", 12000)),
        "response_format": req.response_format,
    }
    if "openrouter.ai" in base_url and policy["judge"].get("reasoning_effort"):
        body["reasoning"] = {"effort": policy["judge"]["reasoning_effort"]}
    else:
        body["temperature"] = 0.0
        rf_copy = copy.deepcopy(req.response_format)
        verdicts_props = (
            (((rf_copy.get("json_schema") or {}).get("schema") or {}).get("properties") or {})
            .get("verdicts", {})
            .get("properties")
        )
        if isinstance(verdicts_props, dict):
            for atom in verdicts_props.values():
                if isinstance(atom, dict) and isinstance(atom.get("properties"), dict):
                    p = atom["properties"]
                    if {"reason", "evidence", "label"} <= set(p):
                        atom["required"] = ["reason", "evidence", "label"]
                        atom["properties"] = {
                            "reason": p["reason"],
                            "evidence": p["evidence"],
                            "label": p["label"],
                        }
        body["response_format"] = rf_copy

    raw_resp = _post_json_with_retry(base_url, api_key, body, timeout=180)
    choices = raw_resp.get("choices") or []
    if not choices:
        raise RuntimeError(f"Empty judge response choices: {raw_resp}")
    content = (choices[0].get("message") or {}).get("content") or ""
    if not content.strip():
        raise RuntimeError(f"Empty judge content: {raw_resp}")
    return content, (raw_resp.get("usage") or {})


class UpstreamParitySolver:
    """Upstream HAEnv gated-query solver executing `solve_gated_round` directly."""

    prompt_mode = "ddx"

    def __init__(
        self,
        *,
        model: str,
        url: str,
        api_key: str,
        temperature: float = 0.0,
        max_tokens: int = 16384,
        max_rounds: int = 32,
        max_calls: int = 256,
        max_points: int = 400,
        timeout: int = 300,
    ) -> None:
        self.name = f"original-parity:{model}"
        self.model = model
        self.url = url
        self.api_key = api_key
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.query = {
            "max_rounds": max_rounds,
            "max_calls": max_calls,
            "max_points": max_points,
        }
        self._failed_turns: list[dict] = []
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.n_api_calls = 0

    def on_case(self, adapter: Any, payload: Any) -> None:
        return

    def solve(self, payload: Any) -> Any:
        from haenv.interaction.query.gated import solve_gated_round
        from haenv.interaction.query.targets import canonical_target

        gc = getattr(self, "gated_context", None) or {}
        if int(gc.get("round") or 1) == 1:
            self._all_menu = list(gc.get("menu") or [])
        else:
            st = getattr(self, "_gq", None)
            if isinstance(st, dict) and st.get("pending") and getattr(self, "_all_menu", None):
                # Upstream haenv@94761fb bug (fixed in 1.6.0): run_gated removes bought
                # items from gc["menu"] before round r+1, causing _purchase_result's
                # canonical_target(target, gc) to fail resolving hashed test-* IDs.
                # Canonicalize pending targets against the full round-1 menu so bought
                # results are returned identically to 1.6.0 and Harbor's ehr_server.py.
                full_gc = {"menu": self._all_menu}
                for p in st["pending"]:
                    p["target"] = canonical_target(p["target"], full_gc)
        return solve_gated_round(self, payload)

    def chat(self, system: str, messages: list[dict], specs: list[Any], tool_choice: str) -> Any:
        from haenv.interaction.query.loop import Turn

        wire_msgs: list[dict[str, Any]] = []
        if system:
            wire_msgs.append({"role": "system", "content": system})
        for m in messages:
            role = m.get("role")
            if role == "tool":
                content = m.get("content")
                if not isinstance(content, str):
                    content = json.dumps(content, ensure_ascii=False)
                wire_msgs.append(
                    {
                        "role": "tool",
                        "tool_call_id": m["tool_call_id"],
                        "name": m.get("name", ""),
                        "content": content,
                    }
                )
            elif role == "assistant":
                native = m.get("native") or {}
                out: dict[str, Any] = {"role": "assistant", "content": m.get("content") or ""}
                calls = native.get("tool_calls") or [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {
                            "name": c["name"],
                            "arguments": c["arguments"]
                            if isinstance(c.get("arguments"), str)
                            else json.dumps(c.get("arguments") or {}, ensure_ascii=False),
                        },
                    }
                    for c in (m.get("tool_calls") or ())
                ]
                if calls:
                    out["tool_calls"] = calls
                wire_msgs.append(out)
            else:
                wire_msgs.append({"role": role, "content": m.get("content") or ""})

        body: dict[str, Any] = {
            "model": self.model,
            "messages": wire_msgs,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        neutral_tools = [s.neutral() for s in (specs or ())]
        if neutral_tools and tool_choice != "none":
            body["tools"] = [{"type": "function", "function": t} for t in neutral_tools]
            body["tool_choice"] = tool_choice

        resp = _post_json_with_retry(self.url, self.api_key, body, timeout=self.timeout)
        self.n_api_calls += 1
        usage = resp.get("usage") or {}
        self.total_input_tokens += int(usage.get("prompt_tokens") or 0)
        self.total_output_tokens += int(usage.get("completion_tokens") or 0)

        choice = (resp.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        raw_calls = msg.get("tool_calls") or []
        calls = []
        native_calls = []
        for idx, tc in enumerate(raw_calls):
            fn = tc.get("function") or {}
            cid = tc.get("id") or f"call_{idx}"
            raw_args = fn.get("arguments")
            try:
                parsed = json.loads(raw_args) if isinstance(raw_args, str) and raw_args.strip() else (raw_args or {})
            except ValueError:
                parsed = raw_args
            calls.append({"id": cid, "name": fn.get("name"), "arguments": parsed})
            native_calls.append(
                {
                    "id": cid,
                    "type": "function",
                    "function": {
                        "name": fn.get("name"),
                        "arguments": raw_args
                        if isinstance(raw_args, str)
                        else json.dumps(raw_args or {}, ensure_ascii=False),
                    },
                }
            )
        native = {"tool_calls": native_calls} if native_calls else None
        return Turn(
            content=msg.get("content") or "",
            tool_calls=calls,
            native=native,
            usage=usage,
            finish=choice.get("finish_reason"),
        )


def _evaluate_one(
    cid: str,
    raw: Any,
    *,
    model: str,
    url: str,
    api_key: str,
    temperature: float,
    max_tokens: int,
    max_rounds: int,
    max_calls: int,
    max_points: int,
    out_dir: Path,
) -> dict[str, Any]:
    from haenv.app.evaluate import _base_row_gated
    from haenv.reporting.tools.m2_raw_correct import raw_correct
    from haenv.scoring.analysis.gates import PROPENSITY_GATES
    from haenv.scoring.management.evidence import obtained_context
    from haenv.scoring.management.judge import make_task, parse_reply, request_for, stub_reply
    from haenv.scoring.management.rubric import derive, load_policy, score
    from haenv.scoring.rows import RunContext
    from haenv_kernel.solver import _extract_json

    t0 = time.time()
    T = int(raw.prediction_context["prediction_time_T"])
    solver = UpstreamParitySolver(
        model=model,
        url=url,
        api_key=api_key,
        temperature=temperature,
        max_tokens=max_tokens,
        max_rounds=max_rounds,
        max_calls=max_calls,
        max_points=max_points,
    )
    ctx = RunContext()
    row = _base_row_gated(cid, solver.name, raw, T, solver, ctx=ctx)

    overall = str(row.get("overall") or "")
    aborted = overall.startswith("ABORT")
    hard_gates = [g for g in (row.get("gates") or []) if str(g).split(":")[0] not in PROPENSITY_GATES]
    propensity_gates = [g for g in (row.get("gates") or []) if str(g).split(":")[0] in PROPENSITY_GATES]

    G = int(not (aborted or hard_gates))
    raw_d = None if aborted else raw_correct(row)
    D = 0 if raw_d is None else int(bool(raw_d))

    st = getattr(solver, "_gq", None) or {}
    trace_messages = list(st.get("msgs") or [])
    answer_text = str(st.get("answered") or "")

    judge_usage: dict[str, Any] = {}
    if G * D == 0:
        details: dict[str, Any] = {
            "case_id": cid,
            "reward": 0.0,
            "S": 0.0,
            "G": G,
            "D": D,
            "Q": None,
            "judge_mode": "skipped_gd_zero",
            "overall": overall,
            "hard_gates": hard_gates,
            "propensity_gates": propensity_gates,
            "dx_listed": row.get("dx_listed"),
            "key_covered": row.get("tool_key_covered"),
            "tests_recall": row.get("tests_recall"),
            "tests_precision": row.get("m2_tests_precision", row.get("tests_precision")),
        }
    else:
        snapshot = (raw.adjudication or {}).get("open_management")
        if not isinstance(snapshot, dict):
            raise ValueError(f"Missing adjudication.open_management for {cid}")
        try:
            answer_obj = _extract_json(answer_text) if answer_text.strip() else {}
        except Exception:  # noqa: BLE001
            answer_obj = {}
        if not isinstance(answer_obj, dict):
            answer_obj = {}

        policy = load_policy()
        obtained = obtained_context(snapshot, trace={"messages": trace_messages}, query_mode=True)
        rubric = derive("m2", snapshot, policy, obtained_evidence=obtained)
        task = make_task(rubric, snapshot, answer_obj, policy, run_id=f"orig-{cid}")
        req = request_for(task, index=1)

        offline = str(os.environ.get("HAENV_OFFLINE_JUDGE", "")).strip().lower() in ("1", "true", "yes")
        if offline:
            judge_mode = "offline_stub"
            raw_judge_text = stub_reply(rubric["items"], answer_obj).raw
        else:
            judge_mode = "live_llm"
            raw_judge_text, judge_usage = _call_live_judge(req, policy)

        verdicts = parse_reply(raw_judge_text, rubric["items"], answer_obj)
        mgmt_summary = score(
            rubric,
            verdicts,
            policy,
            core_decision={"correct": True, "status": "correct", "source": {"kind": "original"}},
        )
        q_val = mgmt_summary.get("quality_score")
        Q = 0.0 if q_val is None else float(q_val)
        S = float(G) * float(D) * (1.0 + Q) / 2.0
        details = {
            "case_id": cid,
            "reward": round(S, 6),
            "S": round(S, 6),
            "G": G,
            "D": D,
            "Q": round(Q, 6),
            "judge_mode": judge_mode,
            "overall": overall,
            "hard_gates": hard_gates,
            "propensity_gates": propensity_gates,
            "dx_listed": row.get("dx_listed"),
            "key_covered": row.get("tool_key_covered"),
            "tests_recall": row.get("tests_recall"),
            "tests_precision": row.get("m2_tests_precision", row.get("tests_precision")),
            "management_summary": mgmt_summary,
            "verdicts": verdicts,
        }

    elapsed = round(time.time() - t0, 2)
    details["solver_usage"] = {
        "prompt_tokens": solver.total_input_tokens,
        "completion_tokens": solver.total_output_tokens,
        "n_api_calls": solver.n_api_calls,
    }
    details["judge_usage"] = {
        "prompt_tokens": int(judge_usage.get("prompt_tokens") or 0),
        "completion_tokens": int(judge_usage.get("completion_tokens") or 0),
    }
    details["elapsed_sec"] = elapsed

    case_dir = out_dir / f"haenv-m2-{cid.lower()}"
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "details.json").write_text(json.dumps(details, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (case_dir / "transcript.json").write_text(
        json.dumps(trace_messages, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (case_dir / "answer.txt").write_text(answer_text, encoding="utf-8")
    return details


def main() -> int:
    ap = argparse.ArgumentParser(description="Run original HAEnv M2 benchmark for Harbor parity")
    ap.add_argument("--pack-dir", type=Path, required=True)
    ap.add_argument("--split", choices=("diagnosis", "parity"), default="parity")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--task-ids", nargs="+", default=None)
    ap.add_argument("--model", required=True)
    ap.add_argument("--base-url", default="")
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--max-tokens", type=int, default=16384)
    ap.add_argument("--max-rounds", type=int, default=32)
    ap.add_argument("--max-calls", type=int, default=256)
    ap.add_argument("--max-points", type=int, default=400)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--output-dir", type=Path, required=True)
    args = ap.parse_args()

    from haenv import i18n
    from haenv.app.artifacts.store import load_cases
    from haenv.interaction.query.adapter import load_pack_adapters
    from haenv.m2.plugin import register as register_m2
    from haenv_kernel.build import build_instance

    batch_dir = _find_batch_dir(args.pack_dir)
    batch_meta_path = batch_dir / "batch.json"
    batch_meta = json.loads(batch_meta_path.read_text(encoding="utf-8")) if batch_meta_path.is_file() else {}
    lang = str(batch_meta.get("language") or "en")

    i18n.set_current(lang)
    register_m2()
    load_pack_adapters()

    cases_path = batch_dir / "cases.jsonl"
    loaded = load_cases(cases_path)
    ordered_ids: list[str] = []
    with cases_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                ordered_ids.append(str(json.loads(line)["case_id"]))

    meta_rows: list[dict] = []
    for cid in ordered_ids:
        raw = loaded[cid]
        T = int(raw.prediction_context["prediction_time_T"])
        _sp, vp = build_instance(raw, T)
        adj = vp.adjudication or {}
        meta_rows.append(
            {
                "case_id": cid,
                "frame": str(((adj.get("m2_frame") or {}).get("frame")) or "F0"),
                "join_gold": str(((adj.get("ddx") or {}).get("join_gold")) or "unified"),
            }
        )

    if args.task_ids:
        wanted = set(args.task_ids)
        selected_rows = [
            r
            for r in meta_rows
            if r["case_id"] in wanted
            or r["case_id"].lower() in wanted
            or f"haenv-m2-{r['case_id'].lower()}" in wanted
        ]
    elif args.split == "parity":
        selected_rows = select_parity_subset(meta_rows, target_n=PARITY_SUBSET_SIZE)
    else:
        selected_rows = meta_rows

    if args.limit is not None:
        selected_rows = selected_rows[: args.limit]

    url, api_key, resolved_model = _resolve_solver_endpoint(args.model, args.base_url)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    results: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    t_start = time.time()

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futs = {
            pool.submit(
                _evaluate_one,
                r["case_id"],
                loaded[r["case_id"]],
                model=resolved_model,
                url=url,
                api_key=api_key,
                temperature=args.temperature,
                max_tokens=args.max_tokens,
                max_rounds=args.max_rounds,
                max_calls=args.max_calls,
                max_points=args.max_points,
                out_dir=args.output_dir,
            ): r["case_id"]
            for r in selected_rows
        }
        for fut in as_completed(futs):
            cid = futs[fut]
            try:
                res = fut.result()
                results[cid] = res
                print(
                    f"[{len(results) + len(errors)}/{len(selected_rows)}] {cid}: "
                    f"S={res['S']:.4f} (G={res['G']}, D={res['D']}, Q={res['Q']}, "
                    f"judge={res['judge_mode']}, {res['elapsed_sec']}s)",
                    flush=True,
                )
            except Exception as exc:  # noqa: BLE001
                tb = traceback.format_exc()
                errors[cid] = f"{type(exc).__name__}: {exc}\n{tb}"
                print(f"[ERROR] {cid}: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)

    ordered_results = [results[r["case_id"]] for r in selected_rows if r["case_id"] in results]
    scores = [float(r["S"]) for r in ordered_results]
    mean_s = sum(scores) / len(scores) if scores else 0.0
    tot_solver_in = sum(r["solver_usage"]["prompt_tokens"] for r in ordered_results)
    tot_solver_out = sum(r["solver_usage"]["completion_tokens"] for r in ordered_results)
    tot_judge_in = sum(r["judge_usage"]["prompt_tokens"] for r in ordered_results)
    tot_judge_out = sum(r["judge_usage"]["completion_tokens"] for r in ordered_results)

    summary = {
        "runner": "original_haenv_m2",
        "model": resolved_model,
        "judge_model": os.environ.get("HAENV_JUDGE_MODEL", ""),
        "split": args.split,
        "n_tasks": len(selected_rows),
        "n_completed": len(ordered_results),
        "n_errors": len(errors),
        "mean_S": round(mean_s, 6),
        "min_S": round(min(scores), 6) if scores else None,
        "max_S": round(max(scores), 6) if scores else None,
        "mean_G": round(sum(r["G"] for r in ordered_results) / len(ordered_results), 6) if ordered_results else 0.0,
        "mean_D": round(sum(r["D"] for r in ordered_results) / len(ordered_results), 6) if ordered_results else 0.0,
        "elapsed_sec": round(time.time() - t_start, 2),
        "tokens": {
            "solver_prompt": tot_solver_in,
            "solver_completion": tot_solver_out,
            "judge_prompt": tot_judge_in,
            "judge_completion": tot_judge_out,
        },
        "case_ids": [r["case_id"] for r in selected_rows],
        "per_case": {
            r["case_id"]: {
                "S": r["S"],
                "G": r["G"],
                "D": r["D"],
                "Q": r["Q"],
                "judge_mode": r["judge_mode"],
                "overall": r["overall"],
                "hard_gates": r["hard_gates"],
            }
            for r in ordered_results
        },
        "errors": errors,
    }
    (args.output_dir / "result.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"DONE original parity: completed={len(ordered_results)}/{len(selected_rows)} "
        f"errors={len(errors)} mean_S={mean_s:.6f} elapsed={summary['elapsed_sec']}s"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
