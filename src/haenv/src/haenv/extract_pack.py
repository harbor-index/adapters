#!/usr/bin/env python3
"""Upstream HAEnv pack extractor (runs in a clean upstream-haenv uv environment).

Reads an audited HAEnv M2 batch (cases.jsonl + message_sources.jsonl + batch.json),
exports gold-stripped public sidecar payloads, hidden verifier gold bundles,
upstream-rendered query prompts, and verified clinical oracle solutions.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace


def _gold_bundle(cid: str, raw, T: int, sp, vp) -> dict:
    """Derive exact gold atoms for one M2 case (mirrors upstream tools/testing/m2_stub_ladder.py)."""
    from haenv.interaction.gated import DECOY_SIGNALS
    from haenv.m2 import core
    from haenv.m2.round2 import alias_sets_of
    from haenv.scoring.tracks import alias_hit_asserted

    adj = vp.adjudication or {}
    ddx = adj.get("ddx") or {}
    plan = adj.get("revision") or {}
    threads = plan.get("threads") if plan else core.threads_of(ddx)
    sets = alias_sets_of(vp)

    th = []
    for i, t in enumerate(threads or []):
        cands = list(t.get("aliases") or []) + [ddx.get("diagnosis") or ""]
        s = sets[i] if i < len(sets) else cands
        say = next((c for c in cands if c and alias_hit_asserted(c, s)), cands[0])
        th.append(
            {
                "say": say,
                "key_signals": list(t.get("key_signals") or []),
                "component": t.get("component"),
            }
        )

    wrong = []
    allsets = [x for s in sets for x in s]
    for r in core.rival_names(ddx):
        if not alias_hit_asserted(r["name"], allsets):
            wrong.append(r["name"])
    if not wrong:
        wrong = ["functional symptoms"]

    keys = [k for t in th for k in t["key_signals"]]
    mt = core.menu_target_of()
    key_targets = {k: mt[k] for k in dict.fromkeys(keys) if k in mt}

    tests = [str(t) for t in (ddx.get("tests") or [])]
    fr = adj.get("m2_frame") or {}
    rcv = core.f3_truth(raw.longitudinal_data or {}, fr, int(T)) if fr.get("frame") == "F3" else None
    join = ddx.get("join_gold") or "unified"

    return {
        "cid": cid,
        "threads": th,
        "wrong_names": wrong,
        "key_targets": key_targets,
        "tests": tests,
        "warranted": bool(adj.get("clinician_action_warranted")),
        "insufficient": bool(ddx.get("insufficient")),
        "join": join,
        "urgency": ddx.get("urgency"),
        "red_flag": bool(adj.get("red_flag_present") or ddx.get("red_flag")),
        "frame": fr.get("frame") or "F0",
        "med_gold": fr.get("med_direction"),
        "drug_gold": fr.get("target_drug"),
        "rcv_gold": (rcv or {}).get("exceeds_rcv"),
        "rcv_stream": fr.get("followed_stream"),
        "ev": [e["evidence_id"] for e in (sp.evidence_ledger or [])][:3],
    }


def _capture_gated_initial_and_purchases(raw, T: int, lang: str) -> dict:
    """Capture exact upstream gated payload, menu, budget, question prompt, and pre-computed menu results."""
    from haenv.interaction.gated import DEFAULT_BUDGET, MAX_ROUNDS, run_gated
    from haenv.interaction.query.gated import _purchase_result
    from haenv.interaction.query.solver import QUERY_SYSTEM, QueryView, protocol_text, render_question
    from haenv.interaction.query.tools import RecordStore, ResultGuard
    from haenv.m2.query import ADAPTER, PROTOCOL

    class _StopCapture(Exception):
        pass

    class _InitialSolver:
        name = "capture"
        prompt_mode = "ddx"
        query = {"max_rounds": 32, "max_calls": 256, "max_points": 400}

        def solve(self, payload):
            self.captured_sp = copy.deepcopy(payload)
            self.captured_gc = copy.deepcopy(getattr(self, "gated_context", None) or {})
            raise _StopCapture()

    init_solver = _InitialSolver()
    try:
        run_gated(raw, int(T), init_solver, budget=DEFAULT_BUDGET, max_rounds=MAX_ROUNDS)
    except _StopCapture:
        pass

    sp0 = init_solver.captured_sp
    gc0 = init_solver.captured_gc
    menu = list(gc0.get("menu") or [])
    budget = float(gc0.get("budget") or 0.0)
    max_rounds = int(gc0.get("max_rounds") or MAX_ROUNDS)

    # Render exact upstream query prompt
    store0 = RecordStore(sp0, int(T), ADAPTER, max_points=400)
    guard = ResultGuard(int(T))
    question, _pid = render_question(init_solver, QueryView(sp0, ADAPTER, store0))
    question += protocol_text(init_solver.query, PROTOCOL)

    from haenv.i18n.runtime import runtime_text

    leak_hits = guard.for_prompt().check(question) + guard.for_prompt().check(runtime_text(QUERY_SYSTEM))
    if leak_hits:
        raise RuntimeError(f"Prompt leak detected for {raw.case_id}: {leak_hits}")

    # Pre-compute ALL menu items' exact purchasable observations in ONE run_gated batch call (budget=1e9)
    class _BuyAllSolver:
        name = "buy-all"
        prompt_mode = "ddx"

        def __init__(self, items: list[dict]):
            self.items = items
            self.done = False
            self.after_sp = None
            self.after_gc = None

        def solve(self, payload):
            if not self.done:
                self.done = True
                qs = [
                    {"target": str(it["target"]), "kind": str(it.get("kind") or "basic_lab")}
                    for it in self.items
                ]
                return SimpleNamespace(
                    _raw={},
                    _raw_text=json.dumps({"queries": qs, "commit": False}, ensure_ascii=False),
                )
            self.after_sp = copy.deepcopy(payload)
            self.after_gc = copy.deepcopy(getattr(self, "gated_context", None) or {})
            raise _StopCapture()

    bs = _BuyAllSolver(menu)
    try:
        run_gated(raw, int(T), bs, budget=1e9, max_rounds=MAX_ROUNDS)
    except _StopCapture:
        pass

    purchasable_results: dict[str, dict] = {}
    after_ld = getattr(bs.after_sp, "longitudinal_data", None) or {} if bs.after_sp is not None else {}
    after_gc = bs.after_gc or {}
    unavail_map = after_gc.get("unavailable_results") or {}
    tmp_store = RecordStore(bs.after_sp, int(T), ADAPTER, max_points=400) if bs.after_sp is not None else None

    for item in menu:
        target = str(item["target"])
        points = None
        unavailable = None
        rendered_purchase_res = None
        pts = after_ld.get(target)
        if pts and tmp_store is not None:
            check_res = _purchase_result(target, tmp_store, after_gc)
            bad = guard.check(check_res)
            if bad:
                raise RuntimeError(f"Purchase leak for {raw.case_id}/{target}: {bad}")
            rendered_purchase_res = check_res
            points = [runtime_text(p) for p in pts if int(p.get("ts", T + 1)) <= int(T)]
        elif target in unavail_map:
            unavailable = str(runtime_text(unavail_map[target]))

        purchasable_results[target] = {
            "found": bool(points),
            "points": points,
            "rendered_result": rendered_purchase_res,
            "unavailable": unavailable,
        }

    sp_dict = runtime_text(
        {
            "case_id": getattr(sp0, "case_id", raw.case_id),
            "user_profile": copy.deepcopy(getattr(sp0, "user_profile", None) or {}),
            "prediction_context": copy.deepcopy(getattr(sp0, "prediction_context", None) or {}),
            "longitudinal_data": copy.deepcopy(getattr(sp0, "longitudinal_data", None) or {}),
            "evidence_ledger": copy.deepcopy(getattr(sp0, "evidence_ledger", None) or []),
        }
    )

    return {
        "sp": sp_dict,
        "menu": menu,
        "budget": budget,
        "max_rounds": max_rounds,
        "rendered_question": question,
        "purchasable_results": purchasable_results,
    }


def _build_oracle(raw, T: int, sp, vp, captured: dict) -> tuple[list[str], dict]:
    """Build deterministic oracle purchases and answer JSON achieving G=1, D=1, Q=1.0."""
    from haenv.scoring.management.evidence import obtained_context
    from haenv.scoring.management.judge import v6_controls
    from haenv.scoring.management.rubric import derive, load_policy

    g = _gold_bundle(str(raw.case_id), raw, int(T), sp, vp)
    menu_by_target = {str(m["target"]): m for m in captured["menu"]}
    budget = float(captured["budget"])

    # Select key signal purchases that fit inside budget (emit opaque target_id shown by get_menu)
    from haenv.i18n.runtime import registry_phrase
    from haenv.interaction.query.targets import target_id

    oracle_orders: list[str] = []
    ordered_canons: list[str] = []
    spent = 0.0
    for target in dict.fromkeys(g["key_targets"].values()):
        item = menu_by_target.get(target)
        if item is None:
            continue
        cost = float(item.get("cost") or 0.0)
        if spent + cost <= budget + 1e-9:
            oracle_orders.append(target_id(target))
            ordered_canons.append(target)
            spent += cost

    # Build differential entries (all gold threads for unified/comorbidity; safe non-lumping for independent)
    ev = g["ev"]
    if g["threads"]:
        diff_entries = [
            {
                "rank": idx + 1,
                "diagnosis": t["say"],
                "certainty": "probable",
                "supporting_evidence": ev[:1],
                "ruled_out_by": None,
            }
            for idx, t in enumerate(g["threads"])
        ]
    else:
        diff_entries = [
            {
                "rank": 1,
                "diagnosis": g["wrong_names"][0],
                "certainty": "possible",
                "supporting_evidence": ev[:1],
                "ruled_out_by": None,
            }
        ]
    if len(diff_entries) < 2:
        diff_entries.append(
            {
                "rank": 2,
                "diagnosis": g["wrong_names"][-1],
                "certainty": "rule_out",
                "supporting_evidence": ev[:1],
                "ruled_out_by": ev[:1] or ["clinical assessment"],
            }
        )

    action_for_urgency = {"🔴": "A5", "🟠": "A4", "🟡": "A3", "🟢": "A1"}
    urg = g.get("urgency") or ("🔴" if g["red_flag"] else "🟡")
    act_cls = "A5" if g["red_flag"] else action_for_urgency.get(urg, "A3")
    if g["warranted"] and act_cls in ("A0", "A1", "A2"):
        act_cls = "A3"

    # Build management_plan using upstream's gold control generator for management-rubric-v7,
    # enriched with accurate obtained case-specific findings (ordered tests + clinical notes)
    # so live LLM-as-a-Judge evaluation satisfies the `explanation` grounding criterion.
    snapshot = (raw.adjudication or {}).get("open_management")
    policy = load_policy()
    purch = captured.get("purchasable_results") or {}
    trace_msgs: list[dict] = [{"role": "user", "content": captured["rendered_question"]}]
    finding_snippets: list[str] = []
    for canon in ordered_canons:
        entry = purch.get(canon) or {}
        if entry.get("rendered_result") is not None:
            trace_msgs.append({"role": "tool", "name": "order_test", "content": entry["rendered_result"]})
        pts = entry.get("points") or []
        for pt in pts[-3:]:
            if not isinstance(pt, dict):
                continue
            label = str(pt.get("name") or registry_phrase(canon) or canon).strip()
            val = pt.get("value", pt.get("result", pt.get("text")))
            if val is None or val == "":
                continue
            unit = f" {pt['unit']}" if pt.get("unit") else ""
            meta_bits = []
            if pt.get("flag"):
                meta_bits.append(f"flag {pt['flag']}")
            if pt.get("ref_low") is not None and pt.get("ref_high") is not None:
                meta_bits.append(f"ref {pt['ref_low']}-{pt['ref_high']}")
            suffix = f" ({', '.join(meta_bits)})" if meta_bits else ""
            finding_snippets.append(f"{label}: {val}{unit}{suffix}")

    symptom_snippets: list[str] = []
    for ev_item in (captured.get("sp") or {}).get("evidence_ledger") or []:
        if not isinstance(ev_item, dict):
            continue
        if ev_item.get("source_type") == "patient_reported_symptom" and ev_item.get("symptom"):
            day = ev_item.get("source_timestamp")
            day_prefix = f"Day {day}: " if day is not None else ""
            symptom_snippets.append(f"{day_prefix}{str(ev_item['symptom']).strip()}")
        if len(symptom_snippets) >= 3:
            break

    obtained = obtained_context(snapshot, trace={"messages": trace_msgs}, query_mode=True)
    rubric = derive("m2", snapshot, policy, obtained_evidence=obtained)
    gold_plan = v6_controls(rubric)["gold_facts"]

    dx_list = ", ".join(t["say"] for t in g["threads"] if t.get("say")) or diff_entries[0]["diagnosis"]
    tests_phrase = ", ".join(t for t in g["tests"] if t) or "the recommended clinical workup"
    if finding_snippets:
        facts_phrase = "; ".join(finding_snippets[:6])
        rationale = (
            f"\nClinical rationale and follow-up for the patient: Your obtained investigation findings ({facts_phrase}) "
            f"support evaluating for {dx_list}, which explains why completing {tests_phrase} and "
            f"reassessing results and symptoms with a clinician within 7 days (or seeking immediate care sooner for "
            f"worsening or emergency symptoms) is recommended without autonomous medication changes."
        )
    elif symptom_snippets:
        facts_phrase = "; ".join(symptom_snippets[:3])
        rationale = (
            f"\nClinical rationale and follow-up for the patient: Your recorded clinical symptoms ({facts_phrase}) "
            f"support evaluating for {dx_list}, which explains why completing {tests_phrase} and "
            f"reassessing results and symptoms with a clinician within 7 days (or seeking immediate care sooner for "
            f"worsening or emergency symptoms) is recommended without autonomous medication changes."
        )
    else:
        rationale = (
            f"\nClinical rationale and follow-up for the patient: Your longitudinal record supports evaluating for "
            f"{dx_list}, which explains why completing {tests_phrase} and reassessing results and symptoms with a "
            f"clinician within 7 days (or seeking immediate care sooner for worsening or emergency symptoms) is "
            f"recommended without autonomous medication changes."
        )
    gold_plan = gold_plan + rationale

    # Avoid triggering med_change_without_clinician gate if warranted is False
    clinician_review = True if g["warranted"] else False
    specific_action = (
        "Arrange clinician evaluation and recommended diagnostic workup without autonomous medication changes."
        if clinician_review
        else "Continue routine monitoring and schedule follow-up review without autonomous medication changes."
    )

    answer: dict = {
        "differential": diff_entries,
        "join_type": g["join"],
        "join_reason": "Synthesized from longitudinal clinical findings and workup results up to index time T.",
        "join_evidence": ev[:2],
        "tests_to_order": [t for t in g["tests"] if t],
        "referral_specialty": [],
        "forecast": {"risk_category": "indeterminate"},
        "action": {
            "selected_action_class": act_cls,
            "specific_action": specific_action,
            "what_not_to_do": ["Do not autonomously initiate, stop, or alter prescription medications"],
            "clinician_review_required": clinician_review,
            "followup_interval": "7d",
        },
        "data_quality": {
            "data_sufficiency": "insufficient_data" if g["insufficient"] else "sufficient",
            "signal_quality": {},
        },
        "cited_evidence": ev[:2],
        "management_plan": gold_plan,
    }

    frame = g["frame"]
    if frame == "F1":
        answer["urgency"] = urg
    elif frame == "F2":
        answer["treatment_plan"] = {
            "target_drug": g["drug_gold"],
            "direction": g["med_gold"],
            "reason": "Consistent with longitudinal clinical record and clinician-supervised adjustment rules.",
        }
    elif frame == "F3" and g["rcv_gold"] is not None:
        answer["followup"] = {
            "indicator": g["rcv_stream"],
            "exceeds_rcv": bool(g["rcv_gold"]),
            "explained_by": "Assessed against reference change value and clinical context.",
            "next_step": "Follow up per protocol.",
        }

    return oracle_orders, answer


def _self_check_oracle(raw, T: int, oracle_orders: list[str], oracle_answer: dict, rendered_question: str) -> dict:
    """Verify in-process that the generated oracle achieves G=1, D=1, Q=1.0 -> S=1.0."""
    from haenv.app.evaluate import _base_row_gated
    from haenv.reporting.tools.m2_raw_correct import raw_correct
    from haenv.runtime.solvers import _to_output
    from haenv.scoring.analysis.gates import PROPENSITY_GATES
    from haenv.scoring.management.evidence import obtained_context
    from haenv.scoring.management.judge import parse_reply, stub_reply
    from haenv.scoring.management.rubric import derive, load_policy, score
    from haenv.scoring.rows import RunContext

    class _CheckSolver:
        name = "oracle-check"
        prompt_mode = "ddx"

        def __init__(self):
            self.bought = False

        def solve(self, payload):
            gc = getattr(self, "gated_context", None) or {}
            if gc and not self.bought and oracle_orders:
                from haenv.interaction.query.targets import canonical_target

                self.bought = True
                kinds = {str(i["target"]): str(i["kind"]) for i in (gc.get("menu") or [])}
                qs = []
                for t in oracle_orders:
                    ct = canonical_target(t, gc)
                    qs.append({"kind": kinds.get(ct, "basic_lab"), "target": ct})
                out = _to_output({}, payload)
                out._raw = {}  # noqa: SLF001
                out._raw_text = json.dumps({"queries": qs, "commit": False}, ensure_ascii=False)  # noqa: SLF001
                return out
            out = _to_output(oracle_answer, payload)
            out._raw_text = json.dumps(oracle_answer, ensure_ascii=False)  # noqa: SLF001
            return out

    row = _base_row_gated(str(raw.case_id), "oracle-check", raw, int(T), _CheckSolver(), ctx=RunContext())
    overall = str(row.get("overall") or "")
    aborted = overall.startswith("ABORT")
    hard_gates = [g for g in (row.get("gates") or []) if str(g).split(":")[0] not in PROPENSITY_GATES]
    G = int(not (aborted or hard_gates))
    raw_d = None if aborted else raw_correct(row)
    D = 0 if raw_d is None else int(bool(raw_d))

    snapshot = (raw.adjudication or {}).get("open_management")
    policy = load_policy()
    obtained = obtained_context(
        snapshot,
        trace={"messages": [{"role": "user", "content": rendered_question}]},
        query_mode=True,
    )
    rubric = derive("m2", snapshot, policy, obtained_evidence=obtained)
    reply = stub_reply(rubric["items"], oracle_answer)
    verdicts = parse_reply(reply.raw, rubric["items"], oracle_answer)
    mgmt = score(
        rubric,
        verdicts,
        policy,
        core_decision={"correct": bool(D), "status": "correct" if D else "wrong", "source": {"kind": "self_check"}},
    )
    Q = float(mgmt.get("quality_score") or 0.0)
    S = float(G) * float(D) * (1.0 + Q) / 2.0
    return {
        "G": G,
        "D": D,
        "Q": Q,
        "S": S,
        "overall": overall,
        "hard_gates": hard_gates,
        "dx_listed": row.get("dx_listed"),
        "key_covered": row.get("tool_key_covered"),
        "safety_hits": mgmt.get("safety_hits"),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Extract audited HAEnv M2 pack cases for Harbor adapter")
    ap.add_argument("--batch-dir", type=Path, required=True, help="Path to built pack batch directory (with cases.jsonl)")
    ap.add_argument("--out-jsonl", type=Path, required=True, help="Destination JSONL file")
    ap.add_argument("--lang", choices=("en", "zh"), default="en")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--task-ids", nargs="+", default=None)
    ap.add_argument("--strict-oracle", action="store_true", help="Fail if any oracle self-check scores < 1.0")
    args = ap.parse_args(argv)

    from haenv import i18n
    from haenv.app.artifacts.store import load_cases
    from haenv.interaction.query.adapter import load_pack_adapters
    from haenv.m2.plugin import register as register_m2
    from haenv_kernel.build import build_instance

    # Respect batch language if recorded in batch.json, else use --lang
    batch_meta_path = args.batch_dir / "batch.json"
    batch_meta = json.loads(batch_meta_path.read_text(encoding="utf-8")) if batch_meta_path.is_file() else {}
    lang = str(batch_meta.get("language") or args.lang)
    i18n.set_current(lang)
    register_m2()
    load_pack_adapters()

    cases_path = args.batch_dir / "cases.jsonl"
    raw_lines: dict[str, str] = {}
    ordered_ids: list[str] = []
    with cases_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\r\n")
            if not line.strip():
                continue
            rec = json.loads(line)
            cid = str(rec["case_id"])
            raw_lines[cid] = line
            ordered_ids.append(cid)

    if args.task_ids:
        wanted = set(args.task_ids)
        ordered_ids = [
            cid
            for cid in ordered_ids
            if cid in wanted or cid.lower() in wanted or f"haenv-m2-{cid.lower()}" in wanted
        ]
    if args.limit is not None:
        ordered_ids = ordered_ids[: args.limit]
    selected_set = set(ordered_ids)
    raw_lines = {cid: raw_lines[cid] for cid in ordered_ids}

    msg_lines: dict[str, str] = {}
    msg_path = args.batch_dir / "message_sources.jsonl"
    if msg_path.is_file():
        with msg_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\r\n")
                if not line.strip():
                    continue
                rec = json.loads(line)
                cid = str(rec["case_id"])
                if cid in selected_set:
                    msg_lines[cid] = line

    loaded = load_cases(cases_path)

    args.out_jsonl.parent.mkdir(parents=True, exist_ok=True)
    failures = []
    with args.out_jsonl.open("w", encoding="utf-8") as fh:
        for cid in ordered_ids:
            raw = loaded[cid]
            T = int(raw.prediction_context["prediction_time_T"])
            sp, vp = build_instance(raw, T)
            captured = _capture_gated_initial_and_purchases(raw, T, lang)
            oracle_orders, oracle_answer = _build_oracle(raw, T, sp, vp, captured)
            check = _self_check_oracle(raw, T, oracle_orders, oracle_answer, captured["rendered_question"])

            if check["S"] < 0.999999:
                failures.append((cid, check))

            adj = vp.adjudication or {}
            frame = str(((adj.get("m2_frame") or {}).get("frame")) or "F0")
            join_gold = str(((adj.get("ddx") or {}).get("join_gold")) or "unified")

            case_public = {
                "case_id": cid,
                "T": T,
                "language": lang,
                "sp": captured["sp"],
                "menu": captured["menu"],
                "budget": captured["budget"],
                "max_rounds": captured["max_rounds"],
                "max_calls": 256,
                "max_points": 400,
                "rendered_question": captured["rendered_question"],
                "purchasable_results": captured["purchasable_results"],
            }
            gold_case = {
                "case_id": cid,
                "language": lang,
                "cases_jsonl_line": raw_lines[cid],
                "message_sources_line": msg_lines.get(cid),
            }
            out_row = {
                "case_id": cid,
                "language": lang,
                "frame": frame,
                "join_gold": join_gold,
                "case_public": case_public,
                "gold_case": gold_case,
                "rendered_question": captured["rendered_question"],
                "oracle_orders": oracle_orders,
                "oracle_answer": oracle_answer,
                "self_check": check,
                "case_sha16": hashlib.sha256(raw_lines[cid].encode("utf-8")).hexdigest()[:16],
            }
            fh.write(json.dumps(out_row, ensure_ascii=False) + "\n")

    print(
        f"[extract_pack] Exported {len(ordered_ids)} M2 cases (lang={lang}) to {args.out_jsonl}; "
        f"oracle_pass={len(ordered_ids) - len(failures)}/{len(ordered_ids)}"
    )
    if failures:
        for cid, chk in failures[:10]:
            print(f"  ORACLE FAIL {cid}: {chk}", file=sys.stderr)
        if args.strict_oracle:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
