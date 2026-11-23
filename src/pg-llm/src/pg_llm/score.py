"""Reproducible upstream ProteinGym-LLM aggregation scorer for Harbor job outputs.

Adapted from ``rohitarorayyc/proteingym-llm`` (MIT License).
Copyright (c) 2026 Rohit Arora, Leo Tianlai Chen, Melissa Du, Debora S. Marks,
and George M. Church.

Matches upstream ``proteingym-llm/src/aggregate.py`` and ``src/analyze.py``:
- validates Harbor ``lock.json``, per-trial ``lock.json``, and ``result.json`` progress stats;
- verifies local task content digests against ``lock.json`` (including relocated jobs via ``--dataset-dir``);
- validates task/agent/model/reward identity consistently across all Harbor records;
- recomputes ground-truth theoretical ceilings from digest-verified ``tests/labels.json``;
- computes two-level UniProt-nested macro per seed first and enforces single-attempt Harbor jobs
  for strict parity scoring (zero-filling all failed/missing planned trials in diagnostic metrics).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import statistics as st
import sys
import tomllib
import uuid
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

SELECTION_TYPES = ["Activity", "Binding", "Expression", "OrganismalFitness", "Stability"]
_VALID_SELECTION_TYPES = frozenset(SELECTION_TYPES)
_VALID_VERIFIER_STATUSES = frozenset({"scored", "missing_answer", "invalid_answer"})
_VALID_SEEDS = frozenset({1, 2, 3})
_VALID_SIZES = frozenset({10, 50, 100})
_TASK_SIZE_RE = re.compile(r"-n(10|50|100)-b[123](?:$|__)")
_SHA256_LOCK_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CANONICAL_ID_RE = re.compile(r"^M\d{2,3}$")
_MAX_ANSWER_BYTES = 16 * 1024 * 1024
_HARBOR_SINGLE_FILES = ("task.toml", "instruction.md", "README.md", "trajectory.json")
_HARBOR_CONTENT_DIRS = ("environment", "tests", "solution", "steps")
_REQUIRED_HASHED_REL_PATHS = (
    "task.toml",
    "instruction.md",
    "environment/Dockerfile",
    "tests/labels.json",
    "tests/grade.py",
    "tests/test.sh",
)
_DEFAULT_IGNORE_NAMES = frozenset({"__pycache__", ".DS_Store"})
_DEFAULT_IGNORE_SUFFIXES = (".pyc", ".swp", ".swo", "~")
_HARBOR_JOB_LOCK_SCHEMA_VERSION = 3
_HARBOR_TRIAL_LOCK_SCHEMA_VERSION = 2
_UNSUPPORTED_REPETITION_KEYS = frozenset({"repetition_id", "attempt_index", "attempt"})
_HARBOR_ENVIRONMENT_TYPES = frozenset(
    {
        "docker",
        "sbx",
        "podman",
        "daytona",
        "e2b",
        "modal",
        "runloop",
        "langsmith",
        "ec2",
        "gke",
        "ack",
        "openshift",
        "novita",
        "apple-container",
        "singularity",
        "islo",
        "tensorlake",
        "cwsandbox",
        "use-computer",
        "cua-cloud",
        "blaxel",
        "opensandbox",
        "beam",
        "skypilot",
        "hf-sandbox",
        "hyperbrowser",
        "vercel",
        "runta",
        "prime",
        "kata",
        "mosaic",
        "smol",
        "sail",
        "kernel",
        "snowflake",
        "boxd",
        "superserve",
        "aca-sandbox",
    }
)
_HARBOR_RESOURCE_MODES = frozenset({"auto", "limit", "request", "guarantee", "ignore"})
_HARBOR_MODEL_API_FORMATS = frozenset(
    {
        "openai_chat_completions",
        "openai_responses",
        "anthropic_messages",
        "google_generate_content",
        "bedrock_converse",
    }
)
_AGENT_SCHEDULER_KEYS = frozenset({"n_concurrent", "concurrency_group"})
_AGENT_FLOAT_KEYS = frozenset(
    {"override_timeout_sec", "override_setup_timeout_sec", "max_timeout_sec"}
)
_AGENT_EMPTY_DEFAULTS: dict[str, Any] = {
    "skip_model_parsing": False,
    "resume_trajectory": False,
    "skills": [],
    "extra_allowed_hosts": [],
    "kwargs": {},
    "mcp_servers": [],
    "env": {},
    "include_logs": [],
    "exclude_logs": [],
}


def _find_unsupported_rep_keys(obj: dict[str, Any]) -> list[str]:
    return sorted(k for k in _UNSUPPORTED_REPETITION_KEYS if k in obj)


def _is_int_strict(val: object) -> bool:
    return isinstance(val, int) and not isinstance(val, bool)


def _to_finite_float(val: object) -> float | None:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    try:
        out = float(val)
    except OverflowError:
        return None
    return out if math.isfinite(out) else None


def _nonempty_str(val: object) -> str | None:
    return val if isinstance(val, str) and val else None


def _slug_assay(assay: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", assay.strip().lower()).strip("-")
    return re.sub(r"-{2,}", "-", slug)


def _is_canonical_id(mid: object) -> bool:
    if not isinstance(mid, str) or not _CANONICAL_ID_RE.fullmatch(mid):
        return False
    num = int(mid[1:])
    return num >= 1 and f"M{num:02d}" == mid


def _rankdata(vals: list[float]) -> list[float]:
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    ranks = [0.0] * len(vals)
    i = 0
    while i < len(vals):
        j = i
        while j + 1 < len(vals) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _spearman(a: list[float], b: list[float]) -> float:
    ra, rb = _rankdata(a), _rankdata(b)
    n = len(a)
    if n < 2:
        return 0.0
    ma, mb = sum(ra) / n, sum(rb) / n
    cov = sum((ra[i] - ma) * (rb[i] - mb) for i in range(n))
    va = math.sqrt(sum((x - ma) ** 2 for x in ra))
    vb = math.sqrt(sum((x - mb) ** 2 for x in rb))
    return cov / (va * vb) if va and vb else 0.0


def nested_macro(
    assay_rho: dict[str, float],
    meta: dict[str, dict[str, Any]],
    group_key: str = "function",
) -> dict[str, dict[str, Any]]:
    """Two-level UniProt-nested macro over per-assay Spearman."""
    cell: dict[tuple[str, str], list[float]] = defaultdict(list)
    for assay, rho in assay_rho.items():
        m = meta.get(assay)
        if m is None or rho is None or not math.isfinite(rho):
            continue
        uni, grp = m.get("uniprot_id"), m.get(group_key)
        if not uni or not grp or grp == "?":
            continue
        cell[(str(uni), str(grp))].append(float(rho))

    by_group_unis: dict[str, list[float]] = defaultdict(list)
    by_group_assays: dict[str, int] = defaultdict(int)
    for (_uni, grp), rhos in cell.items():
        by_group_unis[grp].append(st.mean(rhos))
        by_group_assays[grp] += len(rhos)

    return {
        grp: {
            "mean_rho": st.mean(unis),
            "n_uniprot": len(unis),
            "n_assays": by_group_assays[grp],
        }
        for grp, unis in by_group_unis.items()
    }


def macro_headline(by_selection: dict[str, dict[str, Any]]) -> float | None:
    """Unweighted mean of the canonical 5 selection-type means that are present."""
    vals = [by_selection[s]["mean_rho"] for s in SELECTION_TYPES if s in by_selection]
    return st.mean(vals) if vals else None


def macro_from_assay_rho(
    assay_rho: dict[str, float], meta: dict[str, dict[str, Any]]
) -> tuple[float | None, dict[str, dict[str, Any]]]:
    """Compute (headline, by_selection) for the functional ``SELECTION_TYPES`` slice."""
    by_selection = nested_macro(assay_rho, meta, "function")
    return macro_headline(by_selection), by_selection


def _read_json_raw(path: Path) -> tuple[bool, Any]:
    if path.is_symlink() or not path.is_file():
        return path.exists() or path.is_symlink(), None
    try:
        return True, json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return True, None


def _read_json(path: Path) -> dict[str, Any] | None:
    exists, data = _read_json_raw(path)
    if not exists:
        return None
    return data if isinstance(data, dict) else None


def _read_reward_file(path: Path) -> float | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return None
        val = float(raw)
    except (OSError, UnicodeDecodeError, ValueError, OverflowError):
        return None
    return val if math.isfinite(val) else None


def _normalize_task_key(raw: object) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    name = raw.strip()
    if "__" in name:
        name = name.split("__", 1)[1]
    return name


def _extract_size_from_strings(*candidates: object) -> int | None:
    for item in candidates:
        if not isinstance(item, str) or not item:
            continue
        leaf = Path(item).name if "/" in item or "\\" in item else item
        m = _TASK_SIZE_RE.search(leaf)
        if m:
            return int(m.group(1))
        m_full = _TASK_SIZE_RE.search(item)
        if m_full:
            return int(m_full.group(1))
    return None


def _is_default_ignored(rel: PurePosixPath, *, is_dir: bool = False) -> bool:
    dir_parts = rel.parts if is_dir else rel.parts[:-1]
    if "__pycache__" in dir_parts:
        return True
    for part in rel.parts:
        if part == ".DS_Store" or any(part.endswith(suf) for suf in _DEFAULT_IGNORE_SUFFIXES):
            return True
    return False


def _has_harborignore_files(resolved_root: Path) -> bool:
    for root_str, _dirnames, filenames in os.walk(resolved_root, followlinks=False):
        if ".harborignore" in filenames:
            return True
        if (Path(root_str) / ".harborignore").is_symlink():
            return True
    return False


def _has_user_symlink_component(path: Path) -> Path | None:
    """Return the first user-created symlink component along ``path`` without collapsing '..'."""
    raw_str = os.fspath(path)
    if os.path.isabs(raw_str):
        base_parts: list[str] = []
        rel_parts = [p for p in raw_str.split(os.sep) if p and p != "."]
    else:
        base_parts = [p for p in os.getcwd().split(os.sep) if p and p != "."]
        rel_parts = [p for p in raw_str.split(os.sep) if p and p != "."]

    cur = Path(os.sep)
    for part in (*base_parts, *rel_parts):
        if part == "..":
            cur = cur.parent
            continue
        cand = cur / part
        if cand.is_symlink():
            if (
                sys.platform == "darwin"
                and cand in {Path("/var"), Path("/tmp"), Path("/etc")}
                and cand.resolve() == Path("/private") / cand.name
            ):
                cur = Path("/private") / cand.name
                continue
            return cand
        cur = cand
    return None


def _ensure_no_in_tree_symlinks(task_dir: Path, resolved_root: Path) -> None:
    sym = _has_user_symlink_component(task_dir)
    if sym is not None:
        raise ValueError(f"Task path cannot be or traverse a symlink: {sym} (via {task_dir})")
    if task_dir.is_symlink() or not task_dir.is_dir():
        raise ValueError(f"Task path is not a real directory: {task_dir}")
    for root_str, dirnames, filenames in os.walk(resolved_root, followlinks=False):
        root_path = Path(root_str)
        for dname in dirnames:
            dp = root_path / dname
            if dp.is_symlink():
                raise ValueError(f"Task directory cannot contain symlinks: {dp}")
        for fname in filenames:
            fp = root_path / fname
            if fp.is_symlink():
                raise ValueError(f"Task directory cannot contain symlinks: {fp}")


def _parse_ranking(text: str, ids: list[str]) -> list[str] | None:
    """Extract the JSON ranking matching grader parse_ranking semantics."""
    if not text:
        return None
    opener = re.search(r'"ranking"\s*:\s*\[', text)
    if opener is not None:
        close_idx = text.find("]", opener.end())
        blob = text[opener.end() : close_idx] if close_idx != -1 else text
    else:
        blob = text
    idset = set(ids)
    seen: set[str] = set()
    order: list[str] = []
    for match in re.finditer(r"M\d{1,3}(?!\d)", blob):
        tok = match.group(0)
        norm = f"M{int(tok[1:]):02d}"
        if norm not in idset:
            continue
        if norm in seen:
            return None
        seen.add(norm)
        order.append(norm)
    if len(order) != len(ids):
        return None
    return order


def _read_bounded_answer_text(path: Path, max_bytes: int = _MAX_ANSWER_BYTES) -> str:
    if path.is_symlink():
        raise ValueError(f"answer file cannot be a symlink: {path}")
    st_info = path.stat()
    if not stat.S_ISREG(st_info.st_mode):
        raise ValueError(f"answer path is not a regular file: {path}")
    if st_info.st_size > max_bytes:
        raise ValueError(f"answer file exceeds {max_bytes} byte limit")
    with path.open("rb") as handle:
        raw = handle.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError(f"answer file exceeds {max_bytes} byte limit")
    return raw.decode("utf-8")


def _replay_saved_answer(
    trial_dir: Path,
    prov: dict[str, Any],
    *,
    status: str,
    reported_rho: float | None,
) -> str | None:
    manifest_path = trial_dir / "artifacts" / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.exists() or not manifest_path.is_file():
        return f"{trial_dir.name}: missing or invalid artifacts/manifest.json"
    try:
        raw_m = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return f"{trial_dir.name}: corrupt artifacts/manifest.json ({exc})"
    if not isinstance(raw_m, list):
        return f"{trial_dir.name}: malformed artifacts/manifest.json (expected list)"
    matching = [
        entry
        for entry in raw_m
        if isinstance(entry, dict) and entry.get("source") == "/app/answer.json"
    ]
    if len(matching) != 1:
        return f"{trial_dir.name}: artifacts/manifest.json missing unique /app/answer.json entry"
    manifest_entry = matching[0]

    ans_path = trial_dir / "artifacts" / "app" / "answer.json"
    if ans_path.is_symlink():
        return f"{trial_dir.name}: saved artifact /app/answer.json is a symlink"

    ids = prov["ids"]
    dms_scores = prov["dms_scores"]

    if status == "scored":
        if (
            manifest_entry.get("status") != "ok"
            or manifest_entry.get("type") != "file"
            or manifest_entry.get("destination") != "artifacts/app/answer.json"
        ):
            return f"{trial_dir.name}: artifacts/manifest.json disagrees with scored answer"
        if not ans_path.is_file():
            return f"{trial_dir.name}: missing saved artifact /app/answer.json for scored trial"
        try:
            text = _read_bounded_answer_text(ans_path)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            return f"{trial_dir.name}: unreadable saved /app/answer.json ({exc})"
        ranking = _parse_ranking(text, ids)
        if ranking is None:
            return f"{trial_dir.name}: saved /app/answer.json does not parse into complete ranking"
        pos = {mid: i for i, mid in enumerate(ranking)}
        replayed_rho = _spearman([-pos[mid] for mid in ids], dms_scores)
        if not math.isfinite(replayed_rho):
            return f"{trial_dir.name}: non-finite replayed Spearman score"
        if reported_rho is None or abs(replayed_rho - reported_rho) > 1e-9:
            return (
                f"{trial_dir.name}: saved /app/answer.json replays to rho={replayed_rho:.12g} "
                f"!= reported {reported_rho!r}"
            )
        return None

    if status == "missing_answer":
        if manifest_entry.get("status") == "ok" and manifest_entry.get("type") == "file":
            return f"{trial_dir.name}: artifacts/manifest.json claims /app/answer.json file exists on missing_answer trial"
        if ans_path.is_file() or (ans_path.exists() and not ans_path.is_dir()):
            return f"{trial_dir.name}: unexpected saved /app/answer.json present for missing_answer trial"
        return None

    if (
        manifest_entry.get("status") != "ok"
        or manifest_entry.get("type") != "file"
        or manifest_entry.get("destination") != "artifacts/app/answer.json"
    ):
        return f"{trial_dir.name}: artifacts/manifest.json disagrees with invalid_answer artifact"
    if not ans_path.is_file():
        return f"{trial_dir.name}: missing saved /app/answer.json for invalid_answer trial"
    try:
        text = _read_bounded_answer_text(ans_path)
    except (UnicodeDecodeError, ValueError):
        return None
    except OSError as exc:
        return f"{trial_dir.name}: unreadable saved /app/answer.json ({exc})"
    ranking = _parse_ranking(text, ids)
    if ranking is None:
        return None
    pos = {mid: i for i, mid in enumerate(ranking)}
    replayed_rho = _spearman([-pos[mid] for mid in ids], dms_scores)
    if not math.isfinite(replayed_rho):
        return None
    return f"{trial_dir.name}: saved /app/answer.json replays as valid scored permutation despite status='invalid_answer'"


def _ensure_no_relative_symlinks(root: Path, files: list[Path]) -> None:
    resolved_root = root.resolve()
    for f in files:
        if f.is_symlink():
            raise ValueError(f"Task file cannot be a symlink: {f}")
        try:
            rel_raw = f.relative_to(root)
        except ValueError:
            rel_raw = None
        if rel_raw is not None:
            cur_raw = root
            for part in rel_raw.parts:
                cur_raw = cur_raw / part
                if cur_raw.is_symlink():
                    raise ValueError(f"Task path cannot traverse a symlink: {cur_raw}")
        rel = f.resolve().relative_to(resolved_root)
        cur = resolved_root
        for part in rel.parts:
            cur = cur / part
            if cur.is_symlink():
                raise ValueError(f"Task path cannot traverse a symlink: {cur}")


def _ensure_required_files_hashed(
    resolved_root: Path, hashed_rels: set[str], task_dir: Path
) -> None:
    missing = [rel for rel in _REQUIRED_HASHED_REL_PATHS if rel not in hashed_rels]
    if missing:
        raise ValueError(
            f"Task content hash omitted required task/provenance file(s) {missing} "
            f"(e.g. via missing file or .gitignore/.harborignore): {task_dir}"
        )


def _compute_harbor_task_digest(task_dir: Path) -> str:
    """Compute Harbor Packager.compute_content_hash digest (using Harbor when installed)."""
    sym = _has_user_symlink_component(task_dir)
    if sym is not None:
        raise ValueError(f"Task path cannot be or traverse a symlink: {sym} (via {task_dir})")
    if task_dir.is_symlink() or not task_dir.is_dir():
        raise ValueError(f"Task path is not a real directory: {task_dir}")
    resolved_root = task_dir.resolve()
    _ensure_no_in_tree_symlinks(task_dir, resolved_root)

    try:
        from harbor.publisher.packager import Packager  # type: ignore

        content_hash, collected = Packager.compute_content_hash(resolved_root)
        _ensure_no_relative_symlinks(resolved_root, collected)
        collected_rels = {
            p.resolve().relative_to(resolved_root).as_posix() for p in collected
        }
        _ensure_required_files_hashed(resolved_root, collected_rels, task_dir)
        return f"sha256:{content_hash}"
    except ImportError:
        pass

    gitignore = resolved_root / ".gitignore"
    if gitignore.exists() or gitignore.is_symlink() or _has_harborignore_files(resolved_root):
        raise ValueError(
            f"Task directory contains .gitignore/.harborignore requiring harbor Packager for exact digest: {task_dir}"
        )

    files: list[Path] = []
    for name in _HARBOR_SINGLE_FILES:
        candidate = resolved_root / name
        if candidate.is_symlink():
            raise ValueError(f"Task file cannot be a symlink: {candidate}")
        if candidate.is_file():
            files.append(candidate)

    for subdir in _HARBOR_CONTENT_DIRS:
        base = resolved_root / subdir
        if base.is_symlink():
            raise ValueError(f"Task directory cannot contain symlinks: {base}")
        if base.is_dir():
            for root_str, dirnames, filenames in os.walk(base, followlinks=False):
                root_path = Path(root_str)
                for dname in list(dirnames):
                    dp = root_path / dname
                    if dp.is_symlink():
                        raise ValueError(f"Task directory cannot contain symlinks: {dp}")
                    rel_d = dp.relative_to(resolved_root).as_posix()
                    if _is_default_ignored(PurePosixPath(rel_d), is_dir=True):
                        dirnames.remove(dname)
                for fname in filenames:
                    fp = root_path / fname
                    if fp.is_symlink():
                        raise ValueError(f"Task directory cannot contain symlinks: {fp}")
                    if fp.is_file():
                        files.append(fp)

    rel_entries: list[tuple[str, Path]] = []
    for f in files:
        rel_posix = f.relative_to(resolved_root).as_posix()
        if not _is_default_ignored(PurePosixPath(rel_posix)):
            rel_entries.append((rel_posix, f))
    rel_entries.sort(key=lambda pair: pair[0])
    _ensure_required_files_hashed(
        resolved_root, {rel_posix for rel_posix, _ in rel_entries}, task_dir
    )

    outer = hashlib.sha256()
    for rel_posix, fp in rel_entries:
        h = hashlib.sha256()
        with fp.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                h.update(chunk)
        outer.update(f"{rel_posix}\0{h.hexdigest()}\n".encode())
    return f"sha256:{outer.hexdigest()}"


def _load_verified_task_provenance(task_dir: Path) -> dict[str, Any]:
    """Read metadata and compute theoretical Spearman ceiling from a verified task directory."""
    toml_path = task_dir / "task.toml"
    labels_path = task_dir / "tests" / "labels.json"
    if toml_path.is_symlink() or not toml_path.is_file():
        raise ValueError(f"Missing or symlinked task.toml in {task_dir}")
    if labels_path.is_symlink() or not labels_path.is_file():
        raise ValueError(f"Missing or symlinked tests/labels.json in {task_dir}")

    parsed_toml = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    meta = parsed_toml.get("metadata") if isinstance(parsed_toml, dict) else None
    if not isinstance(meta, dict):
        raise ValueError(f"Missing [metadata] table in {toml_path}")

    labels = json.loads(labels_path.read_text(encoding="utf-8"))
    if not isinstance(labels, dict):
        raise ValueError(f"Invalid JSON object in {labels_path}")

    raw_ids = labels.get("ids")
    raw_variants = labels.get("variants")
    if not isinstance(raw_ids, list) or len(raw_ids) < 2:
        raise ValueError(f"Invalid 'ids' in {labels_path}")
    if not isinstance(raw_variants, list) or len(raw_variants) != len(raw_ids):
        raise ValueError(f"Invalid 'variants' length in {labels_path}")

    ids: list[str] = []
    seen_ids: set[str] = set()
    for mid in raw_ids:
        if not _is_canonical_id(mid) or mid in seen_ids:
            raise ValueError(f"Invalid or duplicate ID in {labels_path}: {mid!r}")
        assert isinstance(mid, str)
        seen_ids.add(mid)
        ids.append(mid)

    score_by_id: dict[str, float] = {}
    for item in raw_variants:
        if not isinstance(item, dict):
            raise ValueError(f"Invalid variant entry in {labels_path}")
        vid = item.get("id")
        if not _is_canonical_id(vid) or vid in score_by_id:
            raise ValueError(f"Invalid or duplicate variant ID in {labels_path}: {vid!r}")
        assert isinstance(vid, str)
        sc = _to_finite_float(item.get("dms_score"))
        if sc is None:
            raise ValueError(f"Invalid dms_score in {labels_path} for {vid!r}")
        score_by_id[vid] = sc

    if set(score_by_id) != seen_ids:
        raise ValueError(f"Mismatched IDs vs variants in {labels_path}")

    dms_scores = [score_by_id[mid] for mid in ids]
    oracle_indices = sorted(range(len(ids)), key=lambda idx: (-dms_scores[idx], ids[idx]))
    oracle_ranking = [ids[idx] for idx in oracle_indices]
    pos = {mid: i for i, mid in enumerate(oracle_ranking)}
    max_rho = _spearman([-pos[mid] for mid in ids], dms_scores)

    assay = meta.get("dms_id")
    seed = meta.get("seed")
    nsize = labels.get("nominal_size", labels.get("size"))
    uniprot = meta.get("uniprot_id")
    sel_type = meta.get("selection_type")
    taxon = meta.get("taxon")
    msa_cat = meta.get("msa_category")
    n_variants = meta.get("n_variants")

    if (
        not isinstance(assay, str)
        or not assay.strip()
        or not _is_int_strict(seed)
        or int(seed) not in _VALID_SEEDS
        or not _is_int_strict(nsize)
        or int(nsize) not in _VALID_SIZES
        or not isinstance(uniprot, str)
        or not uniprot.strip()
        or not isinstance(sel_type, str)
        or sel_type not in _VALID_SELECTION_TYPES
        or not isinstance(taxon, str)
        or not taxon.strip()
        or taxon.strip() == "?"
        or not isinstance(msa_cat, str)
        or not msa_cat.strip()
        or msa_cat.strip() == "?"
        or not _is_int_strict(n_variants)
        or int(n_variants) != len(ids)
        or labels.get("assay") != assay
        or labels.get("seed") != seed
        or labels.get("uniprot_id") != uniprot
        or labels.get("selection_type") != sel_type
        or labels.get("taxon") != taxon
        or labels.get("msa_category") != msa_cat
    ):
        raise ValueError(f"Inconsistent task metadata between task.toml and labels.json in {task_dir}")

    return {
        "task_key": f"{_slug_assay(assay)}-n{int(nsize)}-b{int(seed)}",
        "assay": assay,
        "seed": int(seed),
        "nominal_size": int(nsize),
        "uniprot_id": uniprot,
        "function": sel_type,
        "taxon": taxon,
        "msa_category": msa_cat,
        "n_variants": int(n_variants),
        "max_rho": float(max_rho),
        "ids": ids,
        "dms_scores": dms_scores,
    }


def _is_valid_iso_datetime(val: object) -> bool:
    if not isinstance(val, str) or not val.strip():
        return False
    s = val[:-1] + "+00:00" if val.endswith("Z") else val
    try:
        datetime.fromisoformat(s)
    except ValueError:
        return False
    return True


def _freeze_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(
            sorted((str(k), _freeze_json_value(v)) for k, v in value.items())
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json_value(v) for v in value)
    if isinstance(value, (set, frozenset)):
        return tuple(sorted((_freeze_json_value(v) for v in value), key=repr))
    return value


def _normalize_agent_dict(
    cfg_agent: dict[str, Any],
    *,
    include_scheduler_keys: bool = False,
    exclude_keys: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    norm: dict[str, Any] = {}
    for k, v in cfg_agent.items():
        if k in exclude_keys or v is None:
            continue
        if not include_scheduler_keys and k in _AGENT_SCHEDULER_KEYS:
            continue
        if k in _AGENT_EMPTY_DEFAULTS and v == _AGENT_EMPTY_DEFAULTS[k]:
            continue
        if k in _AGENT_FLOAT_KEYS and (fv := _to_finite_float(v)) is not None:
            norm[k] = fv
        else:
            norm[k] = v
    return norm


def _normalize_agent_config(
    cfg_agent: dict[str, Any], *, include_scheduler_keys: bool = False
) -> str:
    norm = _normalize_agent_dict(
        cfg_agent, include_scheduler_keys=include_scheduler_keys
    )
    return json.dumps(norm, sort_keys=True, separators=(",", ":"))


def _resolve_configured_agent_identity(
    raw_name: str | None, raw_import_path: str | None
) -> tuple[str | None, str | None, str | None]:
    """Resolve Harbor AgentConfig into (configured_agent_id, builtin_agent_name, effective_import_path)."""
    if (
        raw_import_path is None
        and raw_name is not None
        and ":" in raw_name
        and not raw_name.startswith("acp:")
    ):
        return raw_name, None, raw_name
    if raw_name is not None and raw_name.startswith("acp:"):
        return raw_name, "acp", None
    if raw_name is not None:
        return raw_name, raw_name, raw_import_path
    return raw_import_path, None, raw_import_path


def _extract_agent_config_fields(cfg_agent: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(cfg_agent, dict):
        return {
            "agent_name": None,
            "configured_agent_id": None,
            "builtin_agent_name": None,
            "import_path": None,
            "effective_import_path": None,
            "configured_model_name": None,
            "base_url": None,
            "model_api_format": None,
            "skip_model_parsing": False,
            "agent_kwargs": "{}",
            "agent_config_canonical": "{}",
            "agent_config_exact": "{}",
            "has_agent_config": False,
        }
    raw_name = _nonempty_str(cfg_agent.get("name"))
    raw_import_path = _nonempty_str(cfg_agent.get("import_path"))
    configured_agent_id, builtin_agent_name, effective_import_path = (
        _resolve_configured_agent_identity(raw_name, raw_import_path)
    )
    kwargs_obj = cfg_agent.get("kwargs") if isinstance(cfg_agent.get("kwargs"), dict) else {}
    return {
        "agent_name": raw_name,
        "configured_agent_id": configured_agent_id,
        "builtin_agent_name": builtin_agent_name,
        "import_path": raw_import_path,
        "effective_import_path": effective_import_path,
        "configured_model_name": _nonempty_str(cfg_agent.get("model_name")),
        "base_url": _nonempty_str(cfg_agent.get("base_url")),
        "model_api_format": _nonempty_str(cfg_agent.get("model_api_format")),
        "skip_model_parsing": bool(cfg_agent.get("skip_model_parsing", False)),
        "agent_kwargs": json.dumps(kwargs_obj, sort_keys=True),
        "agent_config_canonical": _normalize_agent_config(cfg_agent, include_scheduler_keys=False),
        "agent_config_exact": _normalize_agent_config(cfg_agent, include_scheduler_keys=True),
        "has_agent_config": True,
    }


def _agent_config_tuple(fields: dict[str, Any]) -> tuple[Any, ...]:
    return (
        fields["agent_name"],
        fields["import_path"],
        fields["configured_model_name"],
        fields["base_url"],
        fields["model_api_format"],
        fields["skip_model_parsing"],
        fields["agent_kwargs"],
        fields["agent_config_exact"],
    )


def _canonical_env_key(env: dict[str, Any] | None) -> Any:
    raw = env if isinstance(env, dict) else {"type": "docker"}
    norm: dict[str, Any] = {
        "force_build": False,
        "delete": True,
        "stream": False,
        "cpu_enforcement_policy": "auto",
        "memory_enforcement_policy": "auto",
    }
    for k, v in raw.items():
        if k in {"extra_docker_compose", "suppress_override_warnings"} or v is None:
            continue
        if k in {"kwargs", "env", "extra_allowed_hosts"} and not v:
            continue
        if k in {"cpu_enforcement_policy", "memory_enforcement_policy"} and isinstance(v, str):
            norm[k] = v.lower()
        elif k == "override_cpus" and _to_finite_float(v) is not None:
            norm[k] = float(v)
        else:
            norm[k] = v
    if "type" not in norm and "import_path" not in norm:
        norm["type"] = "docker"
    return _freeze_json_value(norm)


def _canonical_verifier_key(
    ver: dict[str, Any] | None, *, include_env_mode: bool
) -> Any:
    raw = ver if isinstance(ver, dict) else {"disable": False}
    norm: dict[str, Any] = {"disable": bool(raw.get("disable", False))}
    for k, v in raw.items():
        if k == "environment_mode" and not include_env_mode:
            continue
        if v is None:
            continue
        if k in {"include_logs", "exclude_logs", "env", "kwargs", "extra_allowed_hosts"} and not v:
            continue
        if k in {"override_timeout_sec", "max_timeout_sec"} and _to_finite_float(v) is not None:
            norm[k] = float(v)
        else:
            norm[k] = v
    return _freeze_json_value(norm)


def _canonical_source_trial_key(src: Any) -> Any:
    if not isinstance(src, dict):
        return None
    action = src.get("action")
    st_type = src.get("type")
    tid = src.get("trial_id")
    spath = src.get("path")
    ident = str(tid) if tid is not None else (os.path.normpath(str(spath)) if spath is not None else None)
    st_task = src.get("task")
    st_digest = st_task.get("digest") if isinstance(st_task, dict) else None
    return (action, st_type, ident, st_digest)


def _canonical_user_agent_sig(ua: Any) -> Any:
    if ua is None:
        return None
    if not isinstance(ua, dict):
        return ("__invalid_user_agent__",)
    norm = _normalize_agent_dict(
        ua,
        include_scheduler_keys=True,
        exclude_keys=frozenset({"bridge", "user_persona_path", "user_prompt_template_path"}),
    )
    for pkey in ("user_persona_path", "user_prompt_template_path"):
        if ua.get(pkey) is not None:
            norm[pkey] = os.path.normpath(str(ua[pkey]))
    br = ua.get("bridge")
    if isinstance(br, dict):
        br_norm: dict[str, Any] = {}
        if br.get("kind") is not None:
            br_norm["kind"] = br["kind"]
        if br.get("prompt_path") is not None:
            br_norm["prompt_path"] = os.path.normpath(str(br["prompt_path"]))
        if isinstance(br.get("kwargs"), dict) and br["kwargs"]:
            br_norm["kwargs"] = br["kwargs"]
        norm["bridge"] = br_norm
    elif br is not None:
        norm["bridge"] = ("__invalid_bridge__",)
    return _freeze_json_value(norm)


def _exec_config_sig(container: dict[str, Any] | None, *, from_lock: bool) -> Any:
    obj = container if isinstance(container, dict) else {}
    tm = _to_finite_float(obj.get("timeout_multiplier"))
    norm_tm = tm if tm is not None else 1.0
    tmults = tuple(
        float(v) if (v := _to_finite_float(obj.get(k))) is not None else None
        for k in (
            "agent_timeout_multiplier",
            "verifier_timeout_multiplier",
            "agent_setup_timeout_multiplier",
            "environment_build_timeout_multiplier",
        )
    )
    if from_lock:
        raw_extra = obj.get("extra_instructions")
        if isinstance(raw_extra, list):
            extra_sig: Any = tuple(
                ("path", os.path.normpath(str(e["path"])))
                if e.get("path") is not None
                else ("inline", str(e.get("digest")))
                for e in raw_extra
                if isinstance(e, dict)
            )
        else:
            extra_sig = ()
        raw_edc = obj.get("extra_docker_compose")
        if isinstance(raw_edc, list):
            edc_sig: Any = tuple(
                os.path.normpath(str(e["path"]))
                for e in raw_edc
                if isinstance(e, dict) and e.get("path") is not None
            )
        else:
            edc_sig = ()
    else:
        eip = obj.get("extra_instruction_paths")
        ei = obj.get("extra_instructions")
        if eip is not None and (not isinstance(eip, list) or any(not isinstance(p, str) for p in eip)):
            extra_sig = ("__invalid_extra_instruction_paths__",)
        elif ei is not None and (not isinstance(ei, list) or any(not isinstance(t, str) for t in ei)):
            extra_sig = ("__invalid_extra_instructions__",)
        else:
            extra_sig = tuple(
                [("path", os.path.normpath(p)) for p in (eip or [])]
                + [
                    ("inline", f"sha256:{hashlib.sha256(t.encode('utf-8')).hexdigest()}")
                    for t in (ei or [])
                ]
            )
        env_obj = obj.get("environment")
        raw_edc = env_obj.get("extra_docker_compose") if isinstance(env_obj, dict) else None
        if raw_edc is not None and (not isinstance(raw_edc, list) or any(not isinstance(p, str) for p in raw_edc)):
            edc_sig = ("__invalid_edc__",)
        else:
            edc_sig = tuple(os.path.normpath(p) for p in (raw_edc or []))

    src_trial = obj.get("source_trial")
    if src_trial is None:
        src_sig: Any = None
    elif not isinstance(src_trial, dict):
        src_sig = ("__invalid_source_trial__",)
    else:
        src_path = src_trial.get("path")
        src_sig = (
            src_trial.get("action"),
            src_trial.get("type"),
            os.path.normpath(str(src_path)) if src_path is not None else None,
        )

    return (
        bool(obj.get("install_only", False)),
        norm_tm,
        tmults,
        _canonical_env_key(obj.get("environment") if isinstance(obj.get("environment"), dict) else None),
        _canonical_verifier_key(
            obj.get("verifier") if isinstance(obj.get("verifier"), dict) else None,
            include_env_mode=False,
        ),
        _freeze_json_value(obj.get("artifacts") or []),
        _freeze_json_value(obj.get("override_error_policy") or {}),
        _canonical_user_agent_sig(obj.get("user_agent")),
        src_sig,
        extra_sig,
        edc_sig,
    )


def _trial_lock_equality_key(item: dict[str, Any]) -> Any:
    """Pure-stdlib equivalent of Harbor 0.27.0 TrialLock._equality_key."""
    extra_ins = item.get("extra_instructions")
    extra_ins_key = (
        tuple(e.get("digest") for e in extra_ins)
        if isinstance(extra_ins, list)
        else None
    )
    user_agent = item.get("user_agent")
    ua_key = None
    bridge_inputs = item.get("bridge_inputs") if isinstance(item.get("bridge_inputs"), dict) else {}
    if isinstance(user_agent, dict):
        ua_norm = {
            k: v
            for k, v in user_agent.items()
            if k not in {"user_persona_path", "user_prompt_template_path"} and v is not None
        }
        if isinstance(ua_norm.get("bridge"), dict):
            br = dict(ua_norm["bridge"])
            br.pop("prompt_path", None)
            if isinstance(br.get("kwargs"), dict):
                kw = dict(br["kwargs"])
                for bn in bridge_inputs:
                    kw.pop(bn, None)
                br["kwargs"] = kw
            ua_norm["bridge"] = br
        ua_key = _freeze_json_value(ua_norm)
    up_key = (
        item["user_persona"].get("digest")
        if isinstance(item.get("user_persona"), dict)
        else None
    )
    upt_key = (
        item["user_prompt_template"].get("digest")
        if isinstance(item.get("user_prompt_template"), dict)
        else None
    )
    bp_key = (
        item["bridge_prompt"].get("digest")
        if isinstance(item.get("bridge_prompt"), dict)
        else None
    )
    bi_key = tuple(
        sorted(
            (name, lock.get("digest"))
            for name, lock in bridge_inputs.items()
            if isinstance(lock, dict)
        )
    )
    skills_raw = item.get("skills") if isinstance(item.get("skills"), list) else []
    skills_key = tuple(
        (s.get("name"), s.get("digest"))
        for s in skills_raw
        if isinstance(s, dict)
    )
    edc_raw = item.get("extra_docker_compose")
    edc_key = (
        tuple(e.get("digest") for e in edc_raw if isinstance(e, dict))
        if isinstance(edc_raw, list)
        else None
    )
    agent_raw = item.get("agent") if isinstance(item.get("agent"), dict) else {}
    agent_no_skills = {k: v for k, v in agent_raw.items() if k != "skills"}
    return (
        int(item["schema_version"]),
        item["task"].get("digest"),
        bool(item.get("install_only", False)),
        float(item.get("timeout_multiplier", 1.0)),
        float(item["agent_timeout_multiplier"])
        if _to_finite_float(item.get("agent_timeout_multiplier")) is not None
        else None,
        float(item["verifier_timeout_multiplier"])
        if _to_finite_float(item.get("verifier_timeout_multiplier")) is not None
        else None,
        float(item["agent_setup_timeout_multiplier"])
        if _to_finite_float(item.get("agent_setup_timeout_multiplier")) is not None
        else None,
        float(item["environment_build_timeout_multiplier"])
        if _to_finite_float(item.get("environment_build_timeout_multiplier")) is not None
        else None,
        extra_ins_key,
        _normalize_agent_config(agent_no_skills, include_scheduler_keys=True),
        ua_key,
        up_key,
        upt_key,
        bp_key,
        bi_key,
        skills_key,
        _canonical_env_key(item.get("environment") if isinstance(item.get("environment"), dict) else None),
        edc_key,
        _canonical_verifier_key(
            item.get("verifier") if isinstance(item.get("verifier"), dict) else None,
            include_env_mode=True,
        ),
        _freeze_json_value(item.get("artifacts") or []),
        _freeze_json_value(item.get("override_error_policy") or {}),
        _canonical_source_trial_key(item.get("source_trial")),
    )


_ERROR_POLICY_OVERRIDE_KEYS = frozenset(
    {"should_retry", "should_grade", "has_valid_reward"}
)


def _is_valid_uuid_str(val: object) -> bool:
    if not isinstance(val, str):
        return False
    try:
        uuid.UUID(val)
    except ValueError:
        return False
    return True


def _validate_extra_instruction_lock(
    entry: object, label: str, *, require_path: bool = False
) -> str | None:
    if not isinstance(entry, dict):
        return f"{label} entry must be an object"
    digest = entry.get("digest")
    if not isinstance(digest, str) or not _SHA256_LOCK_RE.fullmatch(digest):
        return f"{label} entry missing valid sha256 digest"
    path_val = entry.get("path")
    if require_path:
        if _nonempty_str(path_val) is None:
            return f"{label} entry missing valid path"
    elif path_val is not None and not isinstance(path_val, str):
        return f"{label} entry path must be string or null"
    return None


def _validate_error_policy_override(entry: object, label: str) -> str | None:
    if not isinstance(entry, dict):
        return f"{label} must be an object"
    unknown = set(entry) - _ERROR_POLICY_OVERRIDE_KEYS
    if unknown:
        return f"{label} has unexpected field(s): {sorted(unknown)}"
    for key in _ERROR_POLICY_OVERRIDE_KEYS:
        if entry.get(key) is not None and not isinstance(entry[key], bool):
            return f"{label}.{key} must be boolean or null"
    return None


def _validate_artifact_entry(entry: object, label: str) -> str | None:
    if isinstance(entry, str):
        return None
    if not isinstance(entry, dict):
        return f"{label} entry must be string or object"
    if not isinstance(entry.get("source"), str):
        return f"{label} entry.source must be string"
    for sfield in ("destination", "service"):
        if entry.get(sfield) is not None and not isinstance(entry[sfield], str):
            return f"{label} entry.{sfield} must be string or null"
    if "exclude" in entry:
        ex = entry["exclude"]
        if not isinstance(ex, list) or any(not isinstance(x, str) for x in ex):
            return f"{label} entry.exclude must be list of strings"
    return None


def _validate_task_lock_block(task_info: object, label: str) -> str | None:
    if not isinstance(task_info, dict):
        return f"{label} task must be an object"
    t_type = task_info.get("type")
    if not isinstance(t_type, str) or t_type not in {"local", "git", "package"}:
        return f"{label} has invalid task.type={t_type!r}"
    digest = task_info.get("digest")
    if not isinstance(digest, str) or not _SHA256_LOCK_RE.fullmatch(digest):
        return f"{label} has invalid task.digest={digest!r}"
    if not isinstance(task_info.get("name"), str):
        return f"{label} task.name must be string"
    for sfield in ("version", "source", "path", "git_url", "git_commit_id"):
        if task_info.get(sfield) is not None and not isinstance(task_info[sfield], str):
            return f"{label} task.{sfield} must be string or null"
    return None


def _validate_environment_block(env_info: object, label: str, *, is_lock: bool) -> str | None:
    if not isinstance(env_info, dict):
        return f"{label} environment must be an object"
    etype = env_info.get("type")
    eip = env_info.get("import_path")
    if etype is not None:
        if not isinstance(etype, str) or etype not in _HARBOR_ENVIRONMENT_TYPES:
            return f"{label} environment has invalid type={etype!r}"
    if eip is not None and _nonempty_str(eip) is None:
        return f"{label} environment has invalid import_path={eip!r}"
    if etype is None and eip is None:
        return f"{label} environment missing type or import_path"
    for bfield in ("force_build", "delete", "stream"):
        if bfield in env_info and not isinstance(env_info[bfield], bool):
            return f"{label} environment.{bfield} must be boolean"
    for pfield in ("cpu_enforcement_policy", "memory_enforcement_policy"):
        if pfield in env_info:
            pval = env_info[pfield]
            if not isinstance(pval, str) or pval.lower() not in _HARBOR_RESOURCE_MODES:
                return f"{label} environment.{pfield}={pval!r} is invalid"
    if env_info.get("override_cpus") is not None:
        ocpus = _to_finite_float(env_info["override_cpus"])
        if ocpus is None or ocpus <= 0:
            return f"{label} environment.override_cpus={env_info['override_cpus']!r} is invalid"
    for ifield in ("override_memory_mb", "override_storage_mb", "override_gpus"):
        if env_info.get(ifield) is not None and not _is_int_strict(env_info[ifield]):
            return f"{label} environment.{ifield} must be integer"
    if env_info.get("override_tpu") is not None and not isinstance(env_info["override_tpu"], dict):
        return f"{label} environment.override_tpu must be object"
    if env_info.get("mounts") is not None:
        mounts_val = env_info["mounts"]
        if not isinstance(mounts_val, list):
            return f"{label} environment.mounts must be list or null"
        for mount in mounts_val:
            if (
                not isinstance(mount, dict)
                or mount.get("type") not in {"bind", "volume", "image"}
                or not isinstance(mount.get("source"), str)
                or not isinstance(mount.get("target"), str)
            ):
                return f"{label} environment.mounts entry is invalid"
    for dfield in ("env", "kwargs"):
        if dfield in env_info and not isinstance(env_info[dfield], dict):
            return f"{label} environment.{dfield} must be object"
    if isinstance(env_info.get("env"), dict):
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in env_info["env"].items()):
            return f"{label} environment.env entries must be string pairs"
    for lfield in ("extra_allowed_hosts", "extra_docker_compose"):
        if lfield in env_info:
            lval = env_info[lfield]
            if not isinstance(lval, list) or any(not isinstance(x, str) for x in lval):
                if is_lock and lfield == "extra_docker_compose":
                    return f"{label} environment.extra_docker_compose must be list of path strings"
                return f"{label} environment.{lfield} must be list of strings"
    return None


def _validate_agent_block(agent_info: object, label: str) -> str | None:
    if not isinstance(agent_info, dict):
        return f"{label} agent must be an object"
    for sfield in (
        "name",
        "import_path",
        "model_name",
        "base_url",
        "model_api_key_name",
        "concurrency_group",
        "load_trajectory",
    ):
        if agent_info.get(sfield) is not None and not isinstance(agent_info[sfield], str):
            return f"{label} agent.{sfield} must be string or null"
    maf = agent_info.get("model_api_format")
    if maf is not None and (not isinstance(maf, str) or maf not in _HARBOR_MODEL_API_FORMATS):
        return f"{label} agent.model_api_format={maf!r} is invalid"
    for bfield in ("skip_model_parsing", "resume_trajectory"):
        if bfield in agent_info and not isinstance(agent_info[bfield], bool):
            return f"{label} agent.{bfield} must be boolean"
    if agent_info.get("n_concurrent") is not None:
        nc = agent_info["n_concurrent"]
        if not _is_int_strict(nc) or int(nc) < 1:
            return f"{label} agent.n_concurrent={nc!r} must be >= 1"
    for ffield in ("override_timeout_sec", "override_setup_timeout_sec", "max_timeout_sec"):
        if agent_info.get(ffield) is not None and _to_finite_float(agent_info[ffield]) is None:
            return f"{label} agent.{ffield} must be finite number"
    if "kwargs" in agent_info and not isinstance(agent_info["kwargs"], dict):
        return f"{label} agent.kwargs must be object"
    if "env" in agent_info:
        aenv = agent_info["env"]
        if not isinstance(aenv, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in aenv.items()
        ):
            return f"{label} agent.env must be string map"
    for lfield in ("skills", "extra_allowed_hosts", "include_logs", "exclude_logs"):
        if lfield in agent_info:
            lval = agent_info[lfield]
            if not isinstance(lval, list) or any(not isinstance(x, str) for x in lval):
                return f"{label} agent.{lfield} must be list of strings"
    if "mcp_servers" in agent_info:
        mcps = agent_info["mcp_servers"]
        if not isinstance(mcps, list):
            return f"{label} agent.mcp_servers must be list"
        for server in mcps:
            if not isinstance(server, dict) or not isinstance(server.get("name"), str):
                return f"{label} agent.mcp_servers entry missing string name"
            if "transport" in server and server["transport"] not in {
                "sse",
                "streamable-http",
                "stdio",
            }:
                return f"{label} agent.mcp_servers entry has invalid transport={server['transport']!r}"
    return None


def _validate_user_agent_block(ua: object, label: str) -> str | None:
    err = _validate_agent_block(ua, f"{label} user_agent")
    if err is not None:
        return err
    assert isinstance(ua, dict)
    for sfield in ("user_persona_path", "user_prompt_template_path"):
        if ua.get(sfield) is not None and not isinstance(ua[sfield], str):
            return f"{label} user_agent.{sfield} must be string or null"
    bridge = ua.get("bridge")
    if not isinstance(bridge, dict):
        return f"{label} user_agent.bridge must be object"
    if bridge.get("kind") != "acp":
        return f"{label} user_agent.bridge.kind={bridge.get('kind')!r} != 'acp'"
    if bridge.get("prompt_path") is not None and not isinstance(bridge["prompt_path"], str):
        return f"{label} user_agent.bridge.prompt_path must be string or null"
    if "kwargs" in bridge and not isinstance(bridge["kwargs"], dict):
        return f"{label} user_agent.bridge.kwargs must be object"
    return None


def _validate_source_trial_lock(src: object, label: str) -> str | None:
    if not isinstance(src, dict):
        return f"{label} source_trial must be an object"
    if src.get("action") != "regrade":
        return f"{label} source_trial.action={src.get('action')!r} != 'regrade'"
    if src.get("type") not in {"local", "hub"}:
        return f"{label} source_trial.type={src.get('type')!r} is invalid"
    if src.get("trial_id") is not None and not _is_valid_uuid_str(src["trial_id"]):
        return f"{label} source_trial.trial_id must be valid UUID string"
    if src.get("path") is not None and not isinstance(src["path"], str):
        return f"{label} source_trial.path must be string or null"
    if src.get("task") is not None:
        t_err = _validate_task_lock_block(src["task"], f"{label} source_trial")
        if t_err is not None:
            return t_err
    return None


def _validate_verifier_block(ver_info: object, label: str, *, require_lock: bool) -> str | None:
    if not isinstance(ver_info, dict):
        return f"{label} verifier must be an object"
    if "disable" in ver_info and not isinstance(ver_info["disable"], bool):
        return f"{label} verifier.disable must be boolean"
    if require_lock:
        if ver_info.get("disable") is not False:
            return f"{label} has verifier disabled"
        env_mode = ver_info.get("environment_mode")
        if env_mode != "separate":
            return f"{label} verifier environment_mode={env_mode!r} != 'separate'"
    elif "disable" in ver_info and ver_info["disable"] is not False:
        return f"{label} has verifier disabled or invalid"
    for ffield in ("override_timeout_sec", "max_timeout_sec"):
        if ver_info.get(ffield) is not None and _to_finite_float(ver_info[ffield]) is None:
            return f"{label} verifier.{ffield} must be finite number"
    if ver_info.get("import_path") is not None and not isinstance(ver_info["import_path"], str):
        return f"{label} verifier.import_path must be string or null"
    for lfield in ("include_logs", "exclude_logs", "extra_allowed_hosts"):
        if lfield in ver_info:
            lv = ver_info[lfield]
            if not isinstance(lv, list) or any(not isinstance(x, str) for x in lv):
                return f"{label} verifier.{lfield} must be list of strings"
    if "env" in ver_info:
        venv = ver_info["env"]
        if not isinstance(venv, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in venv.items()
        ):
            return f"{label} verifier.env must be string map"
    if "kwargs" in ver_info and not isinstance(ver_info["kwargs"], dict):
        return f"{label} verifier.kwargs must be object"
    return None


def _model_matches_harbor_policy(
    configured_model: str | None,
    *,
    skip_model_parsing: bool,
    parsed_provider: str | None,
    parsed_model: str | None,
) -> bool:
    if configured_model is None:
        return parsed_provider is None and parsed_model is None
    if parsed_model is None:
        return False
    if skip_model_parsing:
        return parsed_model == configured_model and parsed_provider in {
            "harbor-skip-parsing",
            None,
        }

    # Try Harbor's own model parser when running inside a Harbor environment.
    try:
        from harbor.agents.agent_auth_resolution import parse_model_name  # type: ignore

        expected_provider, expected_model = parse_model_name(
            configured_model, skip_model_parsing=False
        )
        if parsed_model != expected_model:
            return False
        if expected_provider is not None and parsed_provider is not None:
            return parsed_provider == expected_provider
        return True
    except Exception:
        pass

    # Pure-stdlib fallback matching LiteLLM/Harbor slash-prefix provider semantics.
    if "/" in configured_model:
        prefix, tail = configured_model.split("/", 1)
        if parsed_provider is not None and parsed_provider != prefix:
            return False
        return parsed_model in {tail, configured_model}
    return parsed_model == configured_model


def _validate_and_inspect_trial(
    trial_dir: Path,
    trial_res: dict[str, Any] | None,
    trial_cfg: dict[str, Any] | None,
    trial_lock: dict[str, Any] | None,
    trial_lock_issue: str | None,
) -> dict[str, Any]:
    conflicts: list[str] = []
    if trial_lock_issue is not None:
        conflicts.append(trial_lock_issue)

    lock_fields = (
        trial_lock["agent_fields"] if trial_lock is not None else None
    )
    cfg_fields = (
        _extract_agent_config_fields(trial_cfg.get("agent"))
        if isinstance(trial_cfg, dict) and isinstance(trial_cfg.get("agent"), dict)
        else None
    )
    res_cfg_obj = trial_res.get("config") if isinstance(trial_res, dict) else None
    res_cfg_fields = (
        _extract_agent_config_fields(res_cfg_obj.get("agent"))
        if isinstance(res_cfg_obj, dict) and isinstance(res_cfg_obj.get("agent"), dict)
        else None
    )

    present_cfg_sources: list[tuple[str, dict[str, Any]]] = []
    if lock_fields is not None:
        present_cfg_sources.append(("trial.lock", lock_fields))
    if cfg_fields is not None:
        present_cfg_sources.append(("trial.config", cfg_fields))
    if res_cfg_fields is not None:
        present_cfg_sources.append(("result.config", res_cfg_fields))

    if not present_cfg_sources:
        base_fields = _extract_agent_config_fields(None)
        conflicts.append("missing agent config in trial config, result, and lock")
    else:
        base_fields = present_cfg_sources[0][1]
        base_sig = _agent_config_tuple(base_fields)
        for src_name, src_fields in present_cfg_sources[1:]:
            if _agent_config_tuple(src_fields) != base_sig:
                conflicts.append(f"conflicting agent config between {present_cfg_sources[0][0]} and {src_name}")

    present_exec_sources: list[tuple[str, Any]] = []
    if trial_lock is not None:
        present_exec_sources.append(("trial.lock", trial_lock["exec_config_sig"]))
    if isinstance(trial_cfg, dict):
        if "environment" in trial_cfg:
            env_err = _validate_environment_block(trial_cfg["environment"], "trial config.json", is_lock=False)
            if env_err is not None:
                conflicts.append(env_err)
        if "verifier" in trial_cfg:
            ver_err = _validate_verifier_block(trial_cfg["verifier"], "trial config.json", require_lock=False)
            if ver_err is not None:
                conflicts.append(ver_err)
        present_exec_sources.append(("trial.config", _exec_config_sig(trial_cfg, from_lock=False)))
    if isinstance(res_cfg_obj, dict):
        if "environment" in res_cfg_obj:
            env_err = _validate_environment_block(res_cfg_obj["environment"], "result.json config", is_lock=False)
            if env_err is not None:
                conflicts.append(env_err)
        if "verifier" in res_cfg_obj:
            ver_err = _validate_verifier_block(res_cfg_obj["verifier"], "result.json config", require_lock=False)
            if ver_err is not None:
                conflicts.append(ver_err)
        present_exec_sources.append(("result.config", _exec_config_sig(res_cfg_obj, from_lock=False)))

    if len(present_exec_sources) > 1:
        base_exec_src, base_exec_sig = present_exec_sources[0]
        for src_name, exec_sig in present_exec_sources[1:]:
            if exec_sig != base_exec_sig:
                conflicts.append(f"conflicting execution config between {base_exec_src} and {src_name}")

    if trial_lock is not None and trial_lock.get("has_source_trial"):
        lock_src_tid = trial_lock.get("source_trial_id")
        for cfg_lbl, cfg_parent in (
            ("trial config.json", trial_cfg),
            ("result.json config", res_cfg_obj),
        ):
            cfg_src = cfg_parent.get("source_trial") if isinstance(cfg_parent, dict) else None
            if isinstance(cfg_src, dict) and cfg_src.get("trial_id") is not None:
                if str(cfg_src["trial_id"]) != str(lock_src_tid):
                    conflicts.append(
                        f"conflicting source_trial.trial_id between trial.lock and {cfg_lbl}"
                    )

    # Cross-check task identity AND normalized task.path across every present source.
    task_keys_by_src: list[tuple[str, str]] = []
    task_paths_by_src: list[tuple[str, str]] = []
    cfg_task_path: str | None = None
    res_cfg_task_path: str | None = None
    res_tid_path: str | None = None
    if trial_lock is not None:
        task_keys_by_src.append(("trial.lock", trial_lock["task_key"]))
        if trial_lock.get("task_path") is not None:
            task_paths_by_src.append(("trial.lock.path", os.path.normpath(trial_lock["task_path"])))
    if not isinstance(trial_cfg, dict) or not isinstance(trial_cfg.get("agent"), dict) or not isinstance(trial_cfg.get("task"), dict):
        conflicts.append("missing or invalid trial config.json")
    if isinstance(trial_cfg, dict) and isinstance(trial_cfg.get("task"), dict):
        tp = _nonempty_str(trial_cfg["task"].get("path"))
        tn = _normalize_task_key(trial_cfg["task"].get("name"))
        cfg_task_path = tp
        if tp is not None:
            task_keys_by_src.append(("trial.config.path", Path(tp).name))
            task_paths_by_src.append(("trial.config.path", os.path.normpath(tp)))
        if tn is not None:
            task_keys_by_src.append(("trial.config.name", tn))
    if isinstance(trial_cfg, dict) and isinstance(trial_cfg.get("verifier"), dict):
        if "disable" in trial_cfg["verifier"] and trial_cfg["verifier"]["disable"] is not False:
            conflicts.append("trial config.json has verifier disabled or invalid")
    if trial_res is not None and (
        not isinstance(res_cfg_obj, dict)
        or not isinstance(res_cfg_obj.get("agent"), dict)
        or not isinstance(res_cfg_obj.get("task"), dict)
    ):
        conflicts.append("missing or invalid result.json config")
    if isinstance(res_cfg_obj, dict) and isinstance(res_cfg_obj.get("task"), dict):
        tp = _nonempty_str(res_cfg_obj["task"].get("path"))
        tn = _normalize_task_key(res_cfg_obj["task"].get("name"))
        res_cfg_task_path = tp
        if tp is not None:
            task_keys_by_src.append(("result.config.path", Path(tp).name))
            task_paths_by_src.append(("result.config.path", os.path.normpath(tp)))
        if tn is not None:
            task_keys_by_src.append(("result.config.name", tn))
    if isinstance(res_cfg_obj, dict) and isinstance(res_cfg_obj.get("verifier"), dict):
        if "disable" in res_cfg_obj["verifier"] and res_cfg_obj["verifier"]["disable"] is not False:
            conflicts.append("result.json config has verifier disabled or invalid")
    if isinstance(trial_res, dict):
        tid = trial_res.get("task_id")
        if isinstance(tid, dict) and _nonempty_str(tid.get("path")) is not None:
            tp_str = str(tid["path"])
            res_tid_path = tp_str
            task_keys_by_src.append(("result.task_id", Path(tp_str).name))
            task_paths_by_src.append(("result.task_id.path", os.path.normpath(tp_str)))
        tn = _normalize_task_key(trial_res.get("task_name"))
        if tn is not None:
            task_keys_by_src.append(("result.task_name", tn))
        v_mode = trial_res.get("verifier_environment_mode")
        if v_mode != "separate":
            conflicts.append(
                f"result.json verifier_environment_mode={v_mode!r} != 'separate'"
            )

    is_local_task = (trial_lock is not None and trial_lock.get("task_type") == "local") or bool(
        task_paths_by_src
    )
    if is_local_task:
        if (
            isinstance(trial_cfg, dict)
            and isinstance(trial_cfg.get("task"), dict)
            and cfg_task_path is None
        ):
            conflicts.append("trial config.json task missing local path")
        if (
            isinstance(res_cfg_obj, dict)
            and isinstance(res_cfg_obj.get("task"), dict)
            and res_cfg_task_path is None
        ):
            conflicts.append("result.json config.task missing local path")
        if isinstance(trial_res, dict) and res_tid_path is None:
            conflicts.append("result.json task_id missing local path")

    for src_lbl, obj in (
        ("trial.config", trial_cfg),
        ("trial.result", trial_res),
        ("result.config", res_cfg_obj),
    ):
        if isinstance(obj, dict):
            rep_keys = _find_unsupported_rep_keys(obj)
            if rep_keys:
                conflicts.append(
                    f"{src_lbl} contains unsupported synthetic repetition key(s): {rep_keys}"
                )

    distinct_keys = {k for _src, k in task_keys_by_src}
    if len(distinct_keys) > 1:
        conflicts.append(f"conflicting task identity across trial files: {task_keys_by_src}")
    task_key = task_keys_by_src[0][1] if len(distinct_keys) == 1 else None

    distinct_paths = {p for _src, p in task_paths_by_src}
    if len(distinct_paths) > 1:
        conflicts.append(f"conflicting task.path across trial files: {task_paths_by_src}")
    normalized_task_path = task_paths_by_src[0][1] if len(distinct_paths) == 1 else None

    nominal_size = _extract_size_from_strings(task_key, trial_dir.name)
    if trial_lock is not None and nominal_size is not None and trial_lock["nominal_size"] != nominal_size:
        conflicts.append("conflicting task size between trial.lock and task identity")

    agent_version: str | None = None
    parsed_provider: str | None = None
    parsed_model: str | None = None
    observed_agent_name: str | None = None
    schema_errors: list[str] = []
    reward_presence_issues: list[str] = []
    has_exc_pre = False

    if isinstance(trial_res, dict):
        if trial_res.get("exception_info") is not None and not isinstance(
            trial_res.get("exception_info"), dict
        ):
            schema_errors.append("malformed exception_info (expected object or null)")
        has_exc_pre, step_schema_err = _inspect_recorded_exceptions(trial_res)
        if step_schema_err is not None:
            schema_errors.append(step_schema_err)
        if "reward_valid" in trial_res and not isinstance(trial_res["reward_valid"], bool):
            schema_errors.append("non-boolean reward_valid")
        vr = trial_res.get("verifier_result")
        if vr is not None and not isinstance(vr, dict):
            schema_errors.append("malformed verifier_result (expected object or null)")
        elif isinstance(vr, dict) and vr.get("rewards") is not None:
            rewards_obj = vr.get("rewards")
            if not isinstance(rewards_obj, dict):
                schema_errors.append("malformed verifier_result.rewards (expected object or null)")
            else:
                for r_key, r_val in rewards_obj.items():
                    if not isinstance(r_key, str) or _to_finite_float(r_val) is None:
                        schema_errors.append("malformed verifier_result.rewards entry")
                        break
        is_rv_pre, _rew_pre, primary_missing_pre, file_mismatch_pre = _extract_harbor_reward(
            trial_res, trial_dir / "verifier" / "reward.txt", has_exception=has_exc_pre
        )
        if primary_missing_pre and (
            not has_exc_pre or vr is not None or trial_res.get("reward_valid") is True
        ):
            reward_presence_issues.append("missing primary verifier_result.rewards['reward']")
        if is_rv_pre and file_mismatch_pre:
            reward_presence_issues.append(
                "verifier_result.rewards['reward'] disagrees with verifier/reward.txt"
            )

        agent_info = trial_res.get("agent_info")
        if not isinstance(agent_info, dict):
            conflicts.append("result.json missing valid agent_info")
        else:
            observed_agent_name = _nonempty_str(agent_info.get("name"))
            agent_version = _nonempty_str(agent_info.get("version"))
            if observed_agent_name is None:
                conflicts.append("result.json agent_info missing name")
            elif (
                base_fields["builtin_agent_name"] is not None
                and observed_agent_name != base_fields["builtin_agent_name"]
            ):
                conflicts.append(
                    f"agent_info.name={observed_agent_name!r} contradicts config agent={base_fields['builtin_agent_name']!r}"
                )
            model_info = agent_info.get("model_info")
            if model_info is not None:
                if not isinstance(model_info, dict):
                    conflicts.append("result.json agent_info.model_info is not an object")
                else:
                    parsed_model = _nonempty_str(model_info.get("name"))
                    parsed_provider = _nonempty_str(model_info.get("provider"))
            if not _model_matches_harbor_policy(
                base_fields["configured_model_name"],
                skip_model_parsing=base_fields["skip_model_parsing"],
                parsed_provider=parsed_provider,
                parsed_model=parsed_model,
            ):
                conflicts.append(
                    f"agent_info.model_info=({parsed_provider!r}, {parsed_model!r}) contradicts configured model={base_fields['configured_model_name']!r}"
                )

    configured_model = base_fields["configured_model_name"]
    if configured_model is not None:
        canonical_model = configured_model
    elif parsed_provider is not None and parsed_model is not None:
        canonical_model = f"{parsed_provider}/{parsed_model}"
    else:
        canonical_model = parsed_model

    return {
        "trial_dir": trial_dir,
        "trial_name": trial_dir.name,
        "task_key": task_key,
        "task_type": trial_lock["task_type"] if trial_lock is not None else None,
        "task_path": Path(normalized_task_path) if normalized_task_path is not None else None,
        "normalized_task_path": normalized_task_path,
        "locked_digest": trial_lock["task_digest"] if trial_lock is not None else None,
        "equality_key": trial_lock["equality_key"] if trial_lock is not None else None,
        "nominal_size": nominal_size,
        "configured_agent_id": base_fields["configured_agent_id"],
        "builtin_agent_name": base_fields["builtin_agent_name"],
        "agent_name": base_fields["configured_agent_id"] or observed_agent_name,
        "observed_agent_name": observed_agent_name,
        "configured_agent_name": base_fields["agent_name"],
        "import_path": base_fields["import_path"],
        "effective_import_path": base_fields["effective_import_path"],
        "agent_version": agent_version,
        "model_name": canonical_model,
        "configured_model_name": configured_model,
        "parsed_provider": parsed_provider,
        "parsed_model": parsed_model,
        "base_url": base_fields["base_url"],
        "model_api_format": base_fields["model_api_format"],
        "skip_model_parsing": base_fields["skip_model_parsing"],
        "agent_kwargs": base_fields["agent_kwargs"],
        "agent_config_canonical": base_fields["agent_config_canonical"],
        "has_agent_config": base_fields["has_agent_config"],
        "has_trial_lock": trial_lock is not None,
        "has_exception_pre": has_exc_pre,
        "cohort_conflicts": conflicts,
        "schema_errors": schema_errors,
        "reward_presence_issues": reward_presence_issues,
    }


def _reconcile_sig(rec: dict[str, Any]) -> tuple[Any, ...]:
    return (
        rec["task_key"],
        rec["task_type"],
        rec["normalized_task_path"],
        rec["locked_digest"],
        rec["equality_key"],
    )


def _cohort_identity_key(rec: dict[str, Any]) -> tuple[Any, ...]:
    return (
        rec["configured_agent_id"],
        rec["observed_agent_name"],
        rec["effective_import_path"],
        rec["agent_version"],
        rec["model_name"],
        rec["parsed_provider"],
        rec["parsed_model"],
        rec["base_url"],
        rec["model_api_format"],
        rec["skip_model_parsing"],
        rec["agent_kwargs"],
        rec["agent_config_canonical"],
    )


def _validate_lock_entry(item: object, label: str) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(item, dict):
        return None, f"malformed {label}"
    rep_keys = _find_unsupported_rep_keys(item)
    if rep_keys:
        return None, f"{label} contains unsupported synthetic repetition key(s): {rep_keys}"
    schema_ver = item.get("schema_version")
    if not _is_int_strict(schema_ver) or int(schema_ver) != _HARBOR_TRIAL_LOCK_SCHEMA_VERSION:
        return (
            None,
            f"{label} unsupported schema_version={schema_ver!r} (expected {_HARBOR_TRIAL_LOCK_SCHEMA_VERSION})",
        )

    if "install_only" in item:
        if not isinstance(item["install_only"], bool) or item["install_only"] is not False:
            return None, f"{label} install_only={item['install_only']!r} must be false"

    if "timeout_multiplier" in item:
        tm_val = _to_finite_float(item["timeout_multiplier"])
        if tm_val is None:
            return None, f"{label} timeout_multiplier={item['timeout_multiplier']!r} must be finite number"

    for tm_key in (
        "agent_timeout_multiplier",
        "verifier_timeout_multiplier",
        "agent_setup_timeout_multiplier",
        "environment_build_timeout_multiplier",
    ):
        if tm_key in item and item[tm_key] is not None:
            tm_val = _to_finite_float(item[tm_key])
            if tm_val is None:
                return None, f"{label} {tm_key}={item[tm_key]!r} must be finite number"

    task_info = item.get("task")
    agent_info = item.get("agent")
    verifier_info = item.get("verifier")
    env_info = item.get("environment")
    if (
        not isinstance(task_info, dict)
        or not isinstance(agent_info, dict)
        or not isinstance(verifier_info, dict)
        or not isinstance(env_info, dict)
    ):
        return None, f"{label} missing task/agent/environment/verifier block"

    t_err = _validate_task_lock_block(task_info, label)
    if t_err is not None:
        return None, t_err
    env_err = _validate_environment_block(env_info, label, is_lock=True)
    if env_err is not None:
        return None, env_err
    agent_err = _validate_agent_block(agent_info, label)
    if agent_err is not None:
        return None, agent_err
    ver_err = _validate_verifier_block(verifier_info, label, require_lock=True)
    if ver_err is not None:
        return None, ver_err

    t_type = task_info["type"]
    digest = task_info["digest"]
    t_name = _nonempty_str(task_info.get("name"))
    t_path = _nonempty_str(task_info.get("path"))
    if t_name is None or (t_type == "local" and t_path is None):
        return None, f"{label} missing task name or local path"
    t_key = Path(t_path).name if t_path is not None else _normalize_task_key(t_name)
    if not t_key or (t_path is not None and _normalize_task_key(t_name) != t_key):
        return None, f"{label} task name/path mismatch ({t_name!r}, {t_path!r})"
    t_size = _extract_size_from_strings(t_name, t_path)
    if t_size is None:
        return None, f"{label} ({t_key}) has unrecognized task size"

    agent_fields = _extract_agent_config_fields(agent_info)
    if agent_fields["configured_agent_id"] is None:
        return None, f"{label} missing agent name/import_path"

    if item.get("extra_instructions") is not None:
        ei_list = item["extra_instructions"]
        if not isinstance(ei_list, list):
            return None, f"{label} extra_instructions must be a list or null"
        for el in ei_list:
            ei_err = _validate_extra_instruction_lock(
                el, f"{label} extra_instructions", require_path=False
            )
            if ei_err is not None:
                return None, ei_err

    if item.get("extra_docker_compose") is not None:
        edc_list = item["extra_docker_compose"]
        if not isinstance(edc_list, list):
            return None, f"{label} extra_docker_compose must be a list or null"
        for el in edc_list:
            edc_err = _validate_extra_instruction_lock(
                el, f"{label} extra_docker_compose", require_path=True
            )
            if edc_err is not None:
                return None, edc_err

    if "skills" in item:
        skills_val = item["skills"]
        if not isinstance(skills_val, list):
            return None, f"{label} skills must be a list"
        for sk in skills_val:
            if (
                not isinstance(sk, dict)
                or _nonempty_str(sk.get("name")) is None
                or _nonempty_str(sk.get("source")) is None
                or not isinstance(sk.get("digest"), str)
                or not _SHA256_LOCK_RE.fullmatch(sk["digest"])
                or (sk.get("git_url") is not None and not isinstance(sk["git_url"], str))
                or (
                    sk.get("git_commit_id") is not None
                    and not isinstance(sk["git_commit_id"], str)
                )
            ):
                return None, f"{label} skills entry invalid"

    if "bridge_inputs" in item:
        b_inputs = item["bridge_inputs"]
        if not isinstance(b_inputs, dict):
            return None, f"{label} bridge_inputs must be an object"
        for bname, block in b_inputs.items():
            if not isinstance(bname, str):
                return None, f"{label} bridge_inputs keys must be strings"
            bi_err = _validate_extra_instruction_lock(
                block, f"{label} bridge_inputs[{bname!r}]", require_path=False
            )
            if bi_err is not None:
                return None, bi_err

    if item.get("user_agent") is not None:
        ua_err = _validate_user_agent_block(item["user_agent"], label)
        if ua_err is not None:
            return None, ua_err

    for inst_key in ("user_persona", "user_prompt_template", "bridge_prompt"):
        if item.get(inst_key) is not None:
            inst_err = _validate_extra_instruction_lock(
                item[inst_key], f"{label} {inst_key}", require_path=False
            )
            if inst_err is not None:
                return None, inst_err

    if item.get("source_trial") is not None:
        st_err = _validate_source_trial_lock(item["source_trial"], label)
        if st_err is not None:
            return None, st_err

    if "artifacts" in item:
        arts = item["artifacts"]
        if not isinstance(arts, list):
            return None, f"{label} artifacts must be a list"
        for art in arts:
            art_err = _validate_artifact_entry(art, f"{label} artifacts")
            if art_err is not None:
                return None, art_err

    if "override_error_policy" in item:
        oep = item["override_error_policy"]
        if not isinstance(oep, dict):
            return None, f"{label} override_error_policy must be an object"
        for ek, ev in oep.items():
            if not isinstance(ek, str):
                return None, f"{label} override_error_policy keys must be strings"
            ep_err = _validate_error_policy_override(
                ev, f"{label} override_error_policy[{ek!r}]"
            )
            if ep_err is not None:
                return None, ep_err

    if item.get("source_trial") is None and bool(item.get("skills")) != bool(
        agent_info.get("skills")
    ):
        return None, f"{label} resolved skills disagree with agent.skills"

    env_edc_paths = tuple(
        os.path.normpath(p) for p in (env_info.get("extra_docker_compose") or [])
    )
    lock_edc_paths = tuple(
        os.path.normpath(e["path"]) for e in (item.get("extra_docker_compose") or [])
    )
    if env_edc_paths != lock_edc_paths:
        return None, f"{label} extra_docker_compose disagrees with environment.extra_docker_compose"

    try:
        from harbor.models.job.lock import TrialLock  # type: ignore

        TrialLock.model_validate(item)
    except ImportError:
        pass
    except Exception as exc:
        return None, f"{label} failed Harbor TrialLock validation: {exc}"

    src_trial_obj = item.get("source_trial")
    return {
        "task_key": t_key,
        "task_type": t_type,
        "task_path": t_path,
        "normalized_task_path": os.path.normpath(t_path) if t_path is not None else None,
        "task_digest": digest,
        "equality_key": _trial_lock_equality_key(item),
        "exec_config_sig": _exec_config_sig(item, from_lock=True),
        "has_source_trial": src_trial_obj is not None,
        "source_trial_id": (
            str(src_trial_obj["trial_id"])
            if isinstance(src_trial_obj, dict) and src_trial_obj.get("trial_id") is not None
            else None
        ),
        "nominal_size": t_size,
        "agent_fields": agent_fields,
        "locked_digest": digest,
        "configured_agent_id": agent_fields["configured_agent_id"],
        "builtin_agent_name": agent_fields["builtin_agent_name"],
        "agent_name": agent_fields["configured_agent_id"],
        "configured_agent_name": agent_fields["agent_name"],
        "import_path": agent_fields["import_path"],
        "effective_import_path": agent_fields["effective_import_path"],
        "configured_model_name": agent_fields["configured_model_name"],
        "model_name": agent_fields["configured_model_name"],
        "base_url": agent_fields["base_url"],
        "model_api_format": agent_fields["model_api_format"],
        "skip_model_parsing": agent_fields["skip_model_parsing"],
        "agent_kwargs": agent_fields["agent_kwargs"],
        "agent_config_canonical": agent_fields["agent_config_canonical"],
    }, None


def _validate_and_extract_lock(job_path: Path) -> tuple[list[dict[str, Any]] | None, str | None]:
    lock_file = job_path / "lock.json"
    if not lock_file.exists() and not lock_file.is_symlink():
        return None, "missing job lock.json"
    _, data = _read_json_raw(lock_file)
    if not isinstance(data, dict):
        return None, "malformed job lock.json"
    rep_keys = _find_unsupported_rep_keys(data)
    if rep_keys:
        return None, f"job lock.json contains unsupported synthetic repetition key(s): {rep_keys}"
    schema_ver = data.get("schema_version")
    if not _is_int_strict(schema_ver) or int(schema_ver) != _HARBOR_JOB_LOCK_SCHEMA_VERSION:
        return (
            None,
            f"job lock.json unsupported schema_version={schema_ver!r} (expected {_HARBOR_JOB_LOCK_SCHEMA_VERSION})",
        )
    if "created_at" in data and not _is_valid_iso_datetime(data["created_at"]):
        return None, f"job lock.json has invalid created_at={data['created_at']!r}"
    if "harbor" in data:
        hb = data["harbor"]
        if not isinstance(hb, dict):
            return None, "job lock.json harbor metadata must be an object"
        for hs in ("version", "git_commit_hash"):
            if hb.get(hs) is not None and not isinstance(hb[hs], str):
                return None, f"job lock.json harbor.{hs} must be string or null"
        if hb.get("is_editable") is not None and not isinstance(hb["is_editable"], bool):
            return None, "job lock.json harbor.is_editable must be boolean or null"
    n_conc = data.get("n_concurrent_trials")
    if not _is_int_strict(n_conc) or int(n_conc) < 1:
        return None, f"job lock.json invalid or missing n_concurrent_trials={n_conc!r}"
    retry_cfg = data.get("retry")
    if not isinstance(retry_cfg, dict):
        return None, "job lock.json missing valid retry block"
    if "max_retries" in retry_cfg:
        mr = retry_cfg["max_retries"]
        if not _is_int_strict(mr) or int(mr) < 0:
            return None, f"job lock.json invalid retry.max_retries={mr!r}"
    for rfloat in ("wait_multiplier", "min_wait_sec", "max_wait_sec"):
        if rfloat in retry_cfg and _to_finite_float(retry_cfg[rfloat]) is None:
            return None, f"job lock.json invalid retry.{rfloat}={retry_cfg[rfloat]!r}"
    for rlist in ("include_exceptions", "exclude_exceptions"):
        if retry_cfg.get(rlist) is not None:
            rl = retry_cfg[rlist]
            if not isinstance(rl, (list, set)) or any(not isinstance(x, str) for x in rl):
                return None, f"job lock.json invalid retry.{rlist}"
    raw_trials = data.get("trials")
    if not isinstance(raw_trials, list) or not raw_trials:
        return None, "empty or invalid trials list in job lock.json"

    try:
        from harbor.models.job.lock import JobLock  # type: ignore

        JobLock.model_validate(data)
    except ImportError:
        pass
    except Exception as exc:
        return None, f"job lock.json failed Harbor JobLock validation: {exc}"

    planned: list[dict[str, Any]] = []
    for idx, item in enumerate(raw_trials):
        entry, err = _validate_lock_entry(item, f"job lock.json trial #{idx}")
        if err is not None or entry is None:
            return None, err
        planned.append(entry)
    return planned, None


def _validate_trial_lock(trial_dir: Path) -> tuple[dict[str, Any] | None, str | None]:
    lock_file = trial_dir / "lock.json"
    if not lock_file.exists() and not lock_file.is_symlink():
        return None, f"{trial_dir.name}: missing trial lock.json"
    _, data = _read_json_raw(lock_file)
    if not isinstance(data, dict):
        return None, f"{trial_dir.name}: malformed trial lock.json"
    return _validate_lock_entry(data, f"{trial_dir.name}/lock.json")


def _validate_job_result(
    job_path: Path,
) -> tuple[int | None, dict[str, int] | None, list[str]]:
    res_file = job_path / "result.json"
    if not res_file.exists() and not res_file.is_symlink():
        return None, None, ["missing job result.json"]
    issues: list[str] = []
    _, data = _read_json_raw(res_file)
    if not isinstance(data, dict):
        return None, None, ["malformed job result.json"]
    rep_keys = _find_unsupported_rep_keys(data)
    if rep_keys:
        issues.append(f"job result.json contains unsupported synthetic repetition key(s): {rep_keys}")
    raw_tot = data.get("n_total_trials")
    if not _is_int_strict(raw_tot) or int(raw_tot) < 0:
        return None, None, [*issues, "job result.json missing valid n_total_trials"]
    expected_total = int(raw_tot)

    raw_status = data.get("status")
    if raw_status is not None and not isinstance(raw_status, dict):
        issues.append("job result.json has malformed status block")

    raw_stats = data.get("stats")
    if not isinstance(raw_stats, dict):
        issues.append(
            f"job result.json missing valid stats block (Harbor treats 0/{expected_total} as completed)"
        )
        return expected_total, None, issues

    eff_stats = dict(raw_stats)
    if "n_completed_trials" not in eff_stats and "n_trials" in eff_stats:
        eff_stats["n_completed_trials"] = eff_stats["n_trials"]
    if "n_errored_trials" not in eff_stats and "n_errors" in eff_stats:
        eff_stats["n_errored_trials"] = eff_stats["n_errors"]
    stat_vals_ok = True
    if isinstance(raw_status, dict):
        for field in (
            "n_completed_trials",
            "n_errored_trials",
            "n_running_trials",
            "n_pending_trials",
            "n_cancelled_trials",
            "n_retries",
        ):
            if field in raw_status:
                s_val = raw_status[field]
                if not _is_int_strict(s_val) or int(s_val) < 0:
                    issues.append(f"job result.json status has invalid {field}={s_val!r}")
                    stat_vals_ok = False
                elif field not in eff_stats:
                    eff_stats[field] = s_val
                else:
                    e_val = eff_stats[field]
                    if not _is_int_strict(e_val) or int(e_val) != int(s_val):
                        issues.append(
                            f"job result.json stats.{field}={e_val!r} contradicts status.{field}={s_val!r}"
                        )
                        stat_vals_ok = False

    completed = eff_stats.get("n_completed_trials")
    errored = (
        eff_stats.get("n_errored_trials")
        if ("n_errored_trials" in eff_stats or "n_errors" in eff_stats)
        else 0
    )
    running = eff_stats.get("n_running_trials", 0)
    has_pending_key = "n_pending_trials" in eff_stats
    pending_raw = eff_stats.get("n_pending_trials")
    cancelled = eff_stats.get("n_cancelled_trials", 0)

    if "n_retries" not in eff_stats:
        issues.append("job result.json stats missing explicit n_retries")
        stat_vals_ok = False
        retries: Any = None
    else:
        retries = eff_stats.get("n_retries")

    for fname, raw_v in (
        ("n_completed_trials", completed),
        ("n_errored_trials", errored),
        ("n_running_trials", running),
        ("n_cancelled_trials", cancelled),
    ):
        if not _is_int_strict(raw_v) or int(raw_v) < 0:
            issues.append(f"job result.json stats has invalid {fname}={raw_v!r}")
            stat_vals_ok = False
    if "n_retries" in eff_stats and (not _is_int_strict(retries) or int(retries) < 0):
        issues.append(f"job result.json stats has invalid n_retries={retries!r}")
        stat_vals_ok = False
    if has_pending_key and (not _is_int_strict(pending_raw) or int(pending_raw) < 0):
        issues.append(f"job result.json stats has invalid n_pending_trials={pending_raw!r}")
        stat_vals_ok = False

    if not stat_vals_ok:
        return expected_total, None, issues

    c_int = int(completed)
    e_int = int(errored)
    r_int = int(running)
    x_int = int(cancelled)
    rt_int = int(retries)
    expected_pending = expected_total - c_int - r_int
    if c_int > expected_total or expected_pending < 0:
        issues.append(
            f"job result.json stats out of bounds (completed={c_int}, running={r_int}, total={expected_total})"
        )
    if e_int > c_int:
        issues.append(
            f"job result.json stats n_errored_trials={e_int} exceeds n_completed_trials={c_int}"
        )
    if x_int > expected_total:
        issues.append(
            f"job result.json stats n_cancelled_trials={x_int} exceeds n_total_trials={expected_total}"
        )
    if has_pending_key and int(pending_raw) != max(expected_pending, 0):
        issues.append(
            f"job result.json stats n_pending_trials={pending_raw} contradicts total-completed-running={expected_pending}"
        )
    p_int = int(pending_raw) if has_pending_key else max(expected_pending, 0)

    return expected_total, {
        "completed": c_int,
        "errored": e_int,
        "running": r_int,
        "pending": p_int,
        "cancelled": x_int,
        "retries": rt_int,
    }, issues


def _inspect_recorded_exceptions(trial_res: dict[str, Any]) -> tuple[bool, str | None]:
    has_exc = trial_res.get("exception_info") is not None
    steps = trial_res.get("step_results")
    if steps is not None:
        if not isinstance(steps, list):
            return True, f"malformed step_results ({type(steps).__name__}, expected list or null)"
        try:
            from harbor.models.trial.result import StepResult  # type: ignore
        except ImportError:
            StepResult = None  # type: ignore[assignment]
        for idx, step in enumerate(steps):
            if not isinstance(step, dict):
                return True, f"malformed step_results[{idx}] (expected object)"
            step_name = step.get("step_name")
            if not isinstance(step_name, str) or not step_name.strip():
                return True, f"malformed step_results[{idx}] missing valid step_name"
            step_ar = step.get("agent_result")
            if step_ar is not None:
                if not isinstance(step_ar, dict):
                    return True, f"malformed step_results[{idx}].agent_result"
                for tok_f in ("n_input_tokens", "n_cache_tokens", "n_output_tokens"):
                    if step_ar.get(tok_f) is not None and not _is_int_strict(step_ar[tok_f]):
                        return True, f"malformed step_results[{idx}].agent_result.{tok_f}"
                if step_ar.get("cost_usd") is not None and _to_finite_float(step_ar["cost_usd"]) is None:
                    return True, f"malformed step_results[{idx}].agent_result.cost_usd"
                if step_ar.get("model_usage") is not None:
                    mu = step_ar["model_usage"]
                    if not isinstance(mu, dict):
                        return True, f"malformed step_results[{idx}].agent_result.model_usage"
                    for mkey, mval in mu.items():
                        if not isinstance(mkey, str) or not isinstance(mval, dict):
                            return True, f"malformed step_results[{idx}].agent_result.model_usage entry"
                        for mtok in ("n_input_tokens", "n_cache_tokens", "n_output_tokens"):
                            if mtok in mval and not _is_int_strict(mval[mtok]):
                                return True, f"malformed step_results[{idx}].agent_result.model_usage.{mtok}"
                        if mval.get("cost_usd") is not None and _to_finite_float(mval["cost_usd"]) is None:
                            return True, f"malformed step_results[{idx}].agent_result.model_usage.cost_usd"
                if step_ar.get("rollout_details") is not None:
                    rds = step_ar["rollout_details"]
                    if not isinstance(rds, list):
                        return True, f"malformed step_results[{idx}].agent_result.rollout_details"
                    for rd in rds:
                        if not isinstance(rd, dict):
                            return True, f"malformed step_results[{idx}].agent_result.rollout_details entry"
                        for seq_key in ("prompt_token_ids", "completion_token_ids"):
                            if seq_key in rd:
                                seq_v = rd[seq_key]
                                if not isinstance(seq_v, list) or any(
                                    not isinstance(turn, list) or any(not _is_int_strict(t) for t in turn)
                                    for turn in seq_v
                                ):
                                    return True, f"malformed step_results[{idx}].agent_result.rollout_details.{seq_key}"
                        if "logprobs" in rd:
                            lps = rd["logprobs"]
                            if not isinstance(lps, list) or any(
                                not isinstance(turn, list) or any(_to_finite_float(lp) is None for lp in turn)
                                for turn in lps
                            ):
                                return True, f"malformed step_results[{idx}].agent_result.rollout_details.logprobs"
                        if "extra" in rd:
                            ex = rd["extra"]
                            if not isinstance(ex, dict) or any(
                                not isinstance(ek, str) or not isinstance(ev, list)
                                for ek, ev in ex.items()
                            ):
                                return True, f"malformed step_results[{idx}].agent_result.rollout_details.extra"
                if step_ar.get("metadata") is not None and not isinstance(step_ar["metadata"], dict):
                    return True, f"malformed step_results[{idx}].agent_result.metadata"
            for tfield in ("agent_execution", "verifier"):
                t_obj = step.get(tfield)
                if t_obj is not None:
                    if not isinstance(t_obj, dict):
                        return True, f"malformed step_results[{idx}].{tfield}"
                    for dt_key in ("started_at", "finished_at"):
                        if t_obj.get(dt_key) is not None and not _is_valid_iso_datetime(t_obj[dt_key]):
                            return True, f"malformed step_results[{idx}].{tfield}.{dt_key}"
            step_exc = step.get("exception_info")
            if step_exc is not None:
                if not isinstance(step_exc, dict):
                    return True, f"malformed step_results[{idx}].exception_info"
                for req_str in ("exception_type", "exception_message", "exception_traceback"):
                    if not isinstance(step_exc.get(req_str), str):
                        return True, f"malformed step_results[{idx}].exception_info.{req_str}"
                if not _is_valid_iso_datetime(step_exc.get("occurred_at")):
                    return True, f"malformed step_results[{idx}].exception_info.occurred_at"
                if step_exc.get("policy_override") is not None:
                    po_err = _validate_error_policy_override(
                        step_exc["policy_override"],
                        f"step_results[{idx}].exception_info.policy_override",
                    )
                    if po_err is not None:
                        return True, po_err
            step_vr = step.get("verifier_result")
            if step_vr is not None:
                if not isinstance(step_vr, dict):
                    return True, f"malformed step_results[{idx}].verifier_result"
                step_rewards = step_vr.get("rewards")
                if step_rewards is not None:
                    if not isinstance(step_rewards, dict):
                        return True, f"malformed step_results[{idx}].verifier_result.rewards"
                    for rk, rv in step_rewards.items():
                        if not isinstance(rk, str) or _to_finite_float(rv) is None:
                            return True, f"malformed step_results[{idx}].verifier_result.rewards entry"
            if StepResult is not None:
                try:
                    StepResult.model_validate(step)
                except Exception as exc:
                    return True, f"step_results[{idx}] failed Harbor StepResult validation: {exc}"
            if step_exc is not None:
                has_exc = True
    return has_exc, None


def _extract_harbor_reward(
    trial_res: dict[str, Any], reward_path: Path, *, has_exception: bool
) -> tuple[bool, float | None, bool, bool]:
    vr = trial_res.get("verifier_result")
    primary_reward: float | None = None
    if isinstance(vr, dict) and isinstance(vr.get("rewards"), dict) and "reward" in vr["rewards"]:
        primary_reward = _to_finite_float(vr["rewards"].get("reward"))

    primary_missing = primary_reward is None

    if "reward_valid" in trial_res:
        declared_valid = trial_res["reward_valid"] is True
        if declared_valid and primary_missing:
            # Harbor TrialResult.reward_valid is False whenever primary reward is missing.
            return False, None, True, False
        is_valid = declared_valid
    else:
        is_valid = (not has_exception) and (not primary_missing)

    if not is_valid:
        return False, None, primary_missing, False

    assert primary_reward is not None
    file_mismatch = False
    file_reward = _read_reward_file(reward_path)
    if file_reward is None or abs(file_reward - primary_reward) > 1e-9:
        file_mismatch = True

    return True, primary_reward, False, file_mismatch


def _seed_macro_mean(
    by_seed: dict[int, dict[str, float]],
    meta: dict[str, dict[str, Any]],
) -> tuple[float | None, dict[int, float]]:
    seed_scores: dict[int, float] = {}
    for seed in sorted(by_seed):
        assay_rho = by_seed[seed]
        if not assay_rho:
            continue
        score = macro_headline(nested_macro(assay_rho, meta, "function"))
        if score is not None:
            seed_scores[seed] = score
    vals = list(seed_scores.values())
    if not vals:
        return None, seed_scores
    return st.mean(vals), seed_scores


def _details_matches_provenance(rec: dict[str, Any], prov: dict[str, Any] | None) -> bool:
    if prov is None or rec["task_key"] is None:
        return False
    details = rec.get("details")
    if not isinstance(details, dict):
        return False
    assay = details.get("assay")
    seed = details.get("seed")
    nsize = details.get("nominal_size")
    dsize = details.get("size")
    uniprot = details.get("uniprot_id")
    sel_type = details.get("selection_type")
    taxon = details.get("taxon")
    msa_cat = details.get("msa_category")
    n_variants = details.get("n_variants")

    size_consistent = (
        _is_int_strict(nsize)
        and int(nsize) in _VALID_SIZES
        and (dsize is None or (_is_int_strict(dsize) and int(dsize) == int(nsize)))
        and (rec["nominal_size"] is None or int(nsize) == rec["nominal_size"])
    )
    basic_identity_ok = (
        isinstance(assay, str)
        and bool(assay.strip())
        and _is_int_strict(seed)
        and int(seed) in _VALID_SEEDS
        and size_consistent
        and isinstance(uniprot, str)
        and bool(uniprot.strip())
        and isinstance(sel_type, str)
        and sel_type in _VALID_SELECTION_TYPES
        and isinstance(taxon, str)
        and bool(taxon.strip())
        and taxon.strip() != "?"
        and isinstance(msa_cat, str)
        and bool(msa_cat.strip())
        and msa_cat.strip() != "?"
        and _is_int_strict(n_variants)
        and int(n_variants) >= 2
    )
    if not basic_identity_ok:
        return False
    assert isinstance(assay, str) and _is_int_strict(seed) and _is_int_strict(nsize)
    derived_key = f"{_slug_assay(assay)}-n{int(nsize)}-b{int(seed)}"
    if derived_key != rec["task_key"]:
        return False
    return (
        prov["assay"] == assay
        and prov["seed"] == int(seed)
        and prov["nominal_size"] == int(nsize)
        and prov["uniprot_id"] == uniprot
        and prov["function"] == sel_type
        and prov["taxon"] == taxon
        and prov["msa_category"] == msa_cat
        and prov["n_variants"] == int(n_variants)
    )


def _validate_trial_verifier_record(
    rec: dict[str, Any], *, is_unindexed: bool
) -> dict[str, Any]:
    trial_res = rec["trial_res"]
    details = rec["details"]
    trial_dir = rec["trial_dir"]

    schema_errors = rec.get("schema_errors") or []
    reward_presence_issues = rec.get("reward_presence_issues") or []
    has_exception, step_schema_err = _inspect_recorded_exceptions(trial_res)
    is_reward_valid, harbor_reward, primary_reward_missing, file_mismatch = _extract_harbor_reward(
        trial_res, trial_dir / "verifier" / "reward.txt", has_exception=has_exception
    )
    if schema_errors or step_schema_err is not None:
        is_reward_valid = False
        harbor_reward = None

    out: dict[str, Any] = {
        "has_exception": has_exception,
        "is_reward_valid": is_reward_valid,
        "harbor_reward": harbor_reward,
        "parsed_rho": None,
        "verifier_ok": False,
        "status": None,
        "fault_msg": None,
        "at_max": False,
        "exact_1_0": False,
        "tied_below_1": False,
    }

    if schema_errors or step_schema_err is not None:
        err_msg = schema_errors[0] if schema_errors else step_schema_err
        out["fault_msg"] = f"{rec['trial_name']}: {err_msg}"
        return out

    if details is None or is_unindexed or rec["cohort_conflicts"] or rec.get("provenance") is None:
        if (
            not has_exception
            or details is not None
            or rec["cohort_conflicts"]
            or rec.get("provenance") is None
            or reward_presence_issues
        ):
            out["fault_msg"] = (
                f"{rec['trial_name']}: invalid/conflicting metadata, provenance, or identity"
            )
        return out

    status = details.get("status")
    parsed = details.get("parsed")
    rho = _to_finite_float(details.get("spearman_rho"))
    details_reward = _to_finite_float(details.get("reward"))
    max_rho = _to_finite_float(details.get("max_possible_spearman_rho"))
    at_max = details.get("at_theoretical_max")

    if (
        not isinstance(status, str)
        or status not in _VALID_VERIFIER_STATUSES
        or not isinstance(parsed, bool)
    ):
        out["fault_msg"] = (
            f"{rec['trial_name']}: invalid verifier status/parsed ({status!r}, {parsed!r})"
        )
        return out

    prov = rec.get("provenance")
    expected_max_rho = prov["max_rho"] if isinstance(prov, dict) else None
    ceiling_fields_ok = (
        details_reward is not None
        and max_rho is not None
        and -1e-12 <= max_rho <= 1.0 + 1e-12
        and isinstance(at_max, bool)
        and (expected_max_rho is None or abs(max_rho - expected_max_rho) <= 1e-9)
    )
    if not ceiling_fields_ok:
        out["fault_msg"] = (
            f"{rec['trial_name']}: invalid reward or forged theoretical ceiling in details.json"
        )
        return out

    out["status"] = status
    assert details_reward is not None and max_rho is not None and isinstance(at_max, bool)

    if status == "scored" or parsed is True:
        rho_valid = (
            status == "scored"
            and parsed is True
            and rho is not None
            and -1.0 - 1e-12 <= rho <= 1.0 + 1e-12
            and abs(rho) <= max_rho + 1e-9
        )
        if not rho_valid:
            out["fault_msg"] = f"{rec['trial_name']}: invalid spearman_rho vs theoretical max"
            return out

        assert rho is not None
        effective_max = expected_max_rho if expected_max_rho is not None else max_rho
        at_max_expected = abs(rho - effective_max) < 1e-9
        reward_matches = (
            is_reward_valid
            and not has_exception
            and not primary_reward_missing
            and not file_mismatch
            and harbor_reward is not None
            and abs(harbor_reward - rho) <= 1e-9
            and abs(details_reward - rho) <= 1e-9
            and at_max == at_max_expected
        )
        if not reward_matches:
            out["fault_msg"] = f"{rec['trial_name']}: inconsistent scored verifier record"
            return out

        assert isinstance(prov, dict)
        replay_err = _replay_saved_answer(trial_dir, prov, status="scored", reported_rho=rho)
        if replay_err is not None:
            out["fault_msg"] = replay_err
            return out

        out["parsed_rho"] = rho
        out["verifier_ok"] = True
        out["at_max"] = at_max
        out["exact_1_0"] = abs(rho - 1.0) < 1e-9
        out["tied_below_1"] = (not out["exact_1_0"]) and (effective_max < 1.0 - 1e-9)
        return out

    inconsistent = (
        parsed is not False
        or details.get("spearman_rho") is not None
        or at_max is not False
        or primary_reward_missing
        or file_mismatch
        or abs(details_reward) > 1e-9
        or (is_reward_valid and (harbor_reward is None or abs(harbor_reward) > 1e-9))
    )
    if inconsistent:
        out["fault_msg"] = f"{rec['trial_name']}: inconsistent {status} record"
    else:
        assert isinstance(prov, dict)
        replay_err = _replay_saved_answer(trial_dir, prov, status=status, reported_rho=None)
        if replay_err is not None:
            out["fault_msg"] = replay_err
        elif is_reward_valid and not has_exception and harbor_reward is not None:
            out["verifier_ok"] = True
    return out


def score_job(
    job_dir: Path,
    *,
    agent: str | None = None,
    agent_version: str | None = None,
    model: str | None = None,
    size: int | None = None,
    attempt: int | None = None,
    dataset_dir: Path | None = None,
    allow_incomplete: bool = False,
) -> dict[str, Any]:
    """Aggregate Harbor PG-LLM trial outputs with strict manifest, digest, cohort, and verifier checks."""
    job_path = Path(job_dir)
    if not job_path.is_dir():
        raise FileNotFoundError(f"Job directory not found: {job_path}")
    if attempt is not None and attempt < 1:
        raise ValueError(f"--attempt must be >= 1, got {attempt}")
    resolved_dataset_dir: Path | None = None
    if dataset_dir is not None:
        raw_ds = Path(dataset_dir)
        sym = _has_user_symlink_component(raw_ds)
        if sym is not None:
            raise ValueError(f"--dataset-dir cannot be or traverse a symlink: {sym} (via {raw_ds})")
        resolved_dataset_dir = raw_ds
        if not resolved_dataset_dir.is_dir():
            raise FileNotFoundError(f"Dataset directory not found: {resolved_dataset_dir}")

    job_cfg_exists, raw_job_cfg = _read_json_raw(job_path / "config.json")
    coverage_issues: list[str] = []
    if job_cfg_exists:
        if not isinstance(raw_job_cfg, dict):
            coverage_issues.append("malformed job config.json")
        else:
            rep_keys = _find_unsupported_rep_keys(raw_job_cfg)
            if rep_keys:
                coverage_issues.append(
                    f"job config.json contains unsupported synthetic repetition key(s): {rep_keys}"
                )
            if "n_attempts" in raw_job_cfg:
                raw_att = raw_job_cfg.get("n_attempts")
                if not _is_int_strict(raw_att) or int(raw_att) != 1:
                    coverage_issues.append(
                        f"job config.json n_attempts={raw_att!r} != 1; run separate Harbor jobs per parity repetition"
                    )

    discovered_dirs = sorted(
        entry
        for entry in job_path.iterdir()
        if entry.is_dir()
        and not entry.name.startswith(".")
        and any((entry / marker).is_file() for marker in ("result.json", "config.json", "trial.log"))
    )

    filtered = any(v is not None for v in (agent, agent_version, model, size))

    planned_trials, lock_issue = _validate_and_extract_lock(job_path)
    if lock_issue is not None:
        coverage_issues.append(lock_issue)
    expected_total_trials, parsed_job_stats, res_issues = _validate_job_result(job_path)
    coverage_issues.extend(res_issues)

    if (
        planned_trials is not None
        and expected_total_trials is not None
        and len(planned_trials) != expected_total_trials
    ):
        coverage_issues.append(
            f"job lock.json plans {len(planned_trials)} trial(s) but job result.json expects n_total_trials={expected_total_trials}"
        )

    if agent_version is not None:
        coverage_issues.append(
            "planned agent_version is not recorded in lock.json; --agent-version filtering is diagnostic only"
        )

    all_completed_inspected: list[dict[str, Any]] = []
    all_completed_count = 0
    completed_records: list[dict[str, Any]] = []
    excluded_completed: list[dict[str, Any]] = []
    n_incomplete_trials = 0
    matched_discovered = 0
    pre_filter_conflicts: list[str] = []
    pre_filter_structural_issues: list[str] = []

    for trial_dir in discovered_dirs:
        trial_res = _read_json(trial_dir / "result.json")
        trial_cfg = _read_json(trial_dir / "config.json")
        details = _read_json(trial_dir / "verifier" / "details.json")
        trial_lock, trial_lock_issue = _validate_trial_lock(trial_dir)

        inspected = _validate_and_inspect_trial(
            trial_dir, trial_res, trial_cfg, trial_lock, trial_lock_issue
        )
        if trial_res is not None:
            all_completed_count += 1
            all_completed_inspected.append(inspected)

        # Enforce structural/config/schema consistency across ALL trials in the job
        # BEFORE cohort filtering so excluded corrupted trials cannot hide conflicts.
        if inspected["cohort_conflicts"]:
            pre_filter_conflicts.extend(
                f"{trial_dir.name}: {c}" for c in inspected["cohort_conflicts"]
            )

        matches = (
            (agent is None or inspected["configured_agent_id"] == agent)
            and (agent_version is None or inspected["agent_version"] == agent_version)
            and (model is None or inspected["model_name"] == model)
            and (size is None or inspected["nominal_size"] == size)
        )
        if not matches:
            for issue in (*inspected["schema_errors"], *inspected["reward_presence_issues"]):
                pre_filter_structural_issues.append(f"{trial_dir.name}: {issue}")
            if inspected["has_exception_pre"]:
                pre_filter_structural_issues.append(
                    f"{trial_dir.name}: excluded trial recorded an exception"
                )
            if trial_res is not None:
                excluded_completed.append(
                    {
                        **inspected,
                        "started_at": str(trial_res.get("started_at") or ""),
                        "trial_res": trial_res,
                        "trial_cfg": trial_cfg,
                        "details": details,
                    }
                )
            continue

        matched_discovered += 1
        if trial_res is None:
            n_incomplete_trials += 1
            continue

        completed_records.append(
            {
                **inspected,
                "started_at": str(trial_res.get("started_at") or ""),
                "trial_res": trial_res,
                "trial_cfg": trial_cfg,
                "details": details,
            }
        )

    if pre_filter_conflicts:
        coverage_issues.append(
            f"{len(pre_filter_conflicts)} trial identity/config conflict(s) ({'; '.join(pre_filter_conflicts[:3])})"
        )
    if pre_filter_structural_issues:
        coverage_issues.append(
            f"{len(pre_filter_structural_issues)} excluded trial record structure issue(s) ({'; '.join(pre_filter_structural_issues[:3])})"
        )

    agent_cohorts = {_cohort_identity_key(rec) for rec in completed_records}
    non_null_sizes = {rec["nominal_size"] for rec in completed_records if rec["nominal_size"] is not None}
    if len(agent_cohorts) > 1 or len(non_null_sizes) > 1:
        cohort_desc = sorted(
            {
                (
                    rec["agent_name"],
                    rec["agent_version"],
                    rec["model_name"],
                    rec["parsed_provider"],
                    rec["parsed_model"],
                    rec["base_url"],
                    rec["nominal_size"],
                )
                for rec in completed_records
            },
            key=lambda x: tuple(str(v) for v in x),
        )
        raise ValueError(
            f"Multiple effective cohorts found in {job_path}: {cohort_desc}. "
            "Pass --agent, --agent-version, --model, and/or --size to score a single cohort."
        )

    if n_incomplete_trials > 0:
        coverage_issues.append(f"{n_incomplete_trials} incomplete trial directory(s) missing result.json")
    if not filtered and expected_total_trials is not None and len(discovered_dirs) != expected_total_trials:
        coverage_issues.append(
            f"discovered {len(discovered_dirs)} trial(s) but job result expects n_total_trials={expected_total_trials}"
        )

    filtered_planned: list[dict[str, Any]] | None = None
    if planned_trials is not None:
        filtered_planned = [
            p
            for p in planned_trials
            if (agent is None or p["configured_agent_id"] == agent)
            and (model is None or p["model_name"] == model)
            and (size is None or p["nominal_size"] == size)
        ]

    if not discovered_dirs or matched_discovered == 0:
        early_planned_count = (
            len(filtered_planned)
            if filtered_planned is not None
            else (expected_total_trials if (not filtered and expected_total_trials is not None) else 0)
        )
        if not allow_incomplete or early_planned_count == 0:
            msg = (
                f"No trial directories found under {job_path}"
                if not discovered_dirs
                else "No trials matched the requested cohort filters"
            )
            raise ValueError(msg)
        coverage_issues.append(
            f"no {'matching ' if discovered_dirs else ''}trial directories found (0/{early_planned_count} planned)"
        )

    if parsed_job_stats is not None:
        if parsed_job_stats["retries"] > 0:
            coverage_issues.append(
                f"job result.json reports n_retries={parsed_job_stats['retries']} > 0 (strict single-attempt scoring requires 0 retries)"
            )
        if all_completed_count != parsed_job_stats["completed"]:
            coverage_issues.append(
                f"discovered {all_completed_count} completed trial(s) across job contradicting job result.json n_completed_trials={parsed_job_stats['completed']}"
            )
        if expected_total_trials is not None and parsed_job_stats["completed"] != expected_total_trials:
            coverage_issues.append(
                f"job result.json reports n_completed_trials={parsed_job_stats['completed']} != n_total_trials={expected_total_trials}"
            )
        for fname in ("errored", "running", "pending", "cancelled"):
            fval = parsed_job_stats[fname]
            if fval > 0:
                coverage_issues.append(
                    f"job result.json reports n_{fname}_trials={fval} > 0"
                )

    if planned_trials is not None:
        if len(discovered_dirs) != len(planned_trials):
            coverage_issues.append(
                f"discovered {len(discovered_dirs)} trial(s) across job while job lock.json plans {len(planned_trials)}"
            )
        all_planned_sig_counts = Counter(_reconcile_sig(p) for p in planned_trials)
        all_observed_sig_counts = Counter(
            _reconcile_sig(rec)
            for rec in all_completed_inspected
            if rec["task_key"] is not None and rec["locked_digest"] is not None
        )
        missing_job_sigs = all_planned_sig_counts - all_observed_sig_counts
        extra_job_sigs = all_observed_sig_counts - all_planned_sig_counts
        if missing_job_sigs:
            sample_job_missing = [k[0] for k in sorted(missing_job_sigs, key=lambda x: str(x[0]))[:3]]
            coverage_issues.append(
                f"{sum(missing_job_sigs.values())} planned trial(s) across job missing in completed trials (e.g. {sample_job_missing})"
            )
        if extra_job_sigs:
            sample_job_extra = [k[0] for k in sorted(extra_job_sigs, key=lambda x: str(x[0]))[:3]]
            coverage_issues.append(
                f"{sum(extra_job_sigs.values())} trial(s) across job disagree with job lock.json (e.g. {sample_job_extra})"
            )

    if filtered_planned is not None:
        if len(filtered_planned) != matched_discovered:
            coverage_issues.append(
                f"discovered {matched_discovered} matching trial(s) but job lock.json plans {len(filtered_planned)}"
            )
        planned_sig_counts = Counter(_reconcile_sig(p) for p in filtered_planned)
        observed_sig_counts = Counter(
            _reconcile_sig(rec)
            for rec in completed_records
            if rec["task_key"] is not None and rec["locked_digest"] is not None
        )
        missing_sigs = planned_sig_counts - observed_sig_counts
        extra_sigs = observed_sig_counts - planned_sig_counts
        if missing_sigs:
            sample_missing = [k[0] for k in sorted(missing_sigs, key=lambda x: str(x[0]))[:3]]
            coverage_issues.append(
                f"{sum(missing_sigs.values())} planned task/digest/config entry(s) missing in completed trials (e.g. {sample_missing})"
            )
        if extra_sigs:
            sample_extra = [k[0] for k in sorted(extra_sigs, key=lambda x: str(x[0]))[:3]]
            coverage_issues.append(
                f"{sum(extra_sigs.values())} unexpected trial(s) absent from job lock.json (e.g. {sample_extra})"
            )

    # Digest-verify task directories and load authoritative metadata + theoretical ceilings,
    # bound strictly to each task's reconciled lock digest.
    verified_provenance: dict[tuple[str, str], dict[str, Any]] = {}
    provenance_errors: list[str] = []
    digest_cache: dict[Path, tuple[str | None, dict[str, Any] | None, str | None]] = {}

    def _verify_task_source(
        task_key: str, locked_digest: str | None, fallback_path: str | Path | None
    ) -> dict[str, Any] | None:
        if locked_digest is None:
            provenance_errors.append(
                f"{task_key}: missing lock.json content digest (cannot verify task provenance)"
            )
            return None
        candidate: Path | None = (
            (resolved_dataset_dir / task_key)
            if resolved_dataset_dir is not None
            else (Path(fallback_path) if fallback_path is not None else None)
        )
        if candidate is None:
            provenance_errors.append(f"{task_key}: missing local task path (pass --dataset-dir for relocated jobs)")
            return None
        if candidate not in digest_cache:
            try:
                digest = _compute_harbor_task_digest(candidate)
                prov = _load_verified_task_provenance(candidate)
                digest_cache[candidate] = (digest, prov, None)
            except Exception as exc:
                digest_cache[candidate] = (None, None, f"{task_key}: {exc}")
        digest, prov, err = digest_cache[candidate]
        if err is not None or prov is None or digest is None:
            provenance_errors.append(err or f"{task_key}: unreadable task directory")
            return None
        if digest != locked_digest:
            provenance_errors.append(
                f"{task_key}: task content digest mismatch against lock.json ({digest} != {locked_digest})"
            )
            return None
        if prov["task_key"] != task_key:
            provenance_errors.append(f"{task_key}: task.toml metadata key {prov['task_key']!r} mismatch")
            return None
        bound_prov = {**prov, "digest": digest}
        verified_provenance[(task_key, locked_digest)] = bound_prov
        return bound_prov

    if planned_trials is not None:
        for p in planned_trials:
            _verify_task_source(p["task_key"], p["task_digest"], p["task_path"])

    if provenance_errors:
        unique_prov_errs = list(dict.fromkeys(provenance_errors))
        coverage_issues.append(
            f"{len(unique_prov_errs)} task digest/provenance verification failure(s) ({'; '.join(unique_prov_errs[:3])})"
        )

    meta: dict[str, dict[str, Any]] = {}
    planned_cells: Counter[tuple[str, int]] = Counter()
    if filtered_planned is not None:
        for p in filtered_planned:
            prov = verified_provenance.get((p["task_key"], p["task_digest"]))
            if prov is not None:
                assay = prov["assay"]
                meta[assay] = {
                    "uniprot_id": prov["uniprot_id"],
                    "function": prov["function"],
                    "taxon": prov["taxon"],
                    "msa_category": prov["msa_category"],
                }
                planned_cells[(assay, prov["seed"])] += 1

    remaining_filt_slots: Counter[tuple[Any, ...]] = (
        Counter(_reconcile_sig(p) for p in filtered_planned)
        if filtered_planned is not None
        else Counter()
    )
    consumed_filt_slots: Counter[tuple[Any, ...]] = Counter()
    cells: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    unindexed_records: list[dict[str, Any]] = []

    ordered_completed = sorted(
        completed_records,
        key=lambda rec: (rec["started_at"], rec["trial_name"]),
    )
    for rec in ordered_completed:
        sig = _reconcile_sig(rec)
        rec_reconciled = (
            filtered_planned is not None
            and rec["task_key"] is not None
            and rec["locked_digest"] is not None
            and remaining_filt_slots[sig] > 0
        )
        if rec_reconciled:
            remaining_filt_slots[sig] -= 1
            consumed_filt_slots[sig] += 1
            prov = verified_provenance.get((rec["task_key"], rec["locked_digest"]))
        else:
            prov = None

        if _details_matches_provenance(rec, prov) and prov is not None:
            meta[prov["assay"]] = {
                "uniprot_id": prov["uniprot_id"],
                "function": prov["function"],
                "taxon": prov["taxon"],
                "msa_category": prov["msa_category"],
            }
            cells[(prov["assay"], prov["seed"])].append(rec)
            rec["provenance"] = prov
        else:
            unindexed_records.append(rec)
            rec["provenance"] = prov

    excluded_verifier_issues: list[str] = []
    ordered_excluded = sorted(
        excluded_completed,
        key=lambda rec: (rec["started_at"], rec["trial_name"]),
    )
    remaining_excl_slots: Counter[tuple[Any, ...]] = (
        Counter(_reconcile_sig(p) for p in planned_trials) - consumed_filt_slots
        if planned_trials is not None
        else Counter()
    )
    for rec in ordered_excluded:
        sig = _reconcile_sig(rec)
        rec_reconciled = (
            planned_trials is not None
            and rec["task_key"] is not None
            and rec["locked_digest"] is not None
            and remaining_excl_slots[sig] > 0
        )
        if rec_reconciled:
            remaining_excl_slots[sig] -= 1
            prov = verified_provenance.get((rec["task_key"], rec["locked_digest"]))
        else:
            prov = None

        prov_ok = _details_matches_provenance(rec, prov)
        excl_rec = {**rec, "provenance": prov}
        v_res = _validate_trial_verifier_record(
            excl_rec,
            is_unindexed=(not rec_reconciled or not prov_ok),
        )
        if v_res["fault_msg"] is not None:
            excluded_verifier_issues.append(v_res["fault_msg"])
        elif (
            not v_res["verifier_ok"]
            or not v_res["is_reward_valid"]
            or v_res["has_exception"]
            or v_res["status"] != "scored"
        ):
            excluded_verifier_issues.append(
                f"{rec['trial_name']}: excluded trial failed strict verification "
                f"(status={v_res['status']!r}, reward_valid={v_res['is_reward_valid']}, "
                f"exception={v_res['has_exception']})"
            )

    if excluded_verifier_issues:
        coverage_issues.append(
            f"{len(excluded_verifier_issues)} excluded trial verifier issue(s) "
            f"({'; '.join(excluded_verifier_issues[:3])})"
        )

    for key in cells:
        cells[key].sort(key=lambda rec: (rec["started_at"], rec["trial_name"]))

    rep_counts = {len(v) for v in cells.values()} if cells else {0}
    has_duplicate_cells = bool(cells and max(rep_counts) > 1)

    if attempt is not None:
        coverage_issues.append(
            "positional --attempt selection is diagnostic only because Harbor does not record stable repetition IDs (use separate Harbor jobs per repetition)"
        )
        selected_records: list[dict[str, Any]] = []
        selected_cells: dict[tuple[str, int], list[dict[str, Any]]] = {}
        missing_attempt = 0
        for key, cell_trials in sorted(cells.items()):
            if len(cell_trials) >= attempt:
                chosen = cell_trials[attempt - 1]
                selected_records.append(chosen)
                selected_cells[key] = [chosen]
            else:
                missing_attempt += 1
        if missing_attempt > 0:
            coverage_issues.append(f"{missing_attempt} (assay, seed) cell(s) lack attempt {attempt}")
        selected_records.extend(unindexed_records)
        n_repetitions = 1 if selected_cells else 0
    else:
        if has_duplicate_cells:
            coverage_issues.append(
                f"duplicate trials in (assay, seed) cells ({sorted(rep_counts)}); run separate Harbor jobs per parity repetition"
            )
        selected_records = [*completed_records]
        selected_cells = cells
        n_repetitions = max(rep_counts) if cells else 0

    n_planned_trials = (
        len(filtered_planned)
        if filtered_planned is not None
        else (
            expected_total_trials
            if (not filtered and expected_total_trials is not None)
            else (len(selected_records) + n_incomplete_trials)
        )
    )

    counts: dict[str, int] = {
        "n_planned_trials": n_planned_trials,
        "n_trials": len(selected_records) + n_incomplete_trials,
        "n_completed_trials": len(selected_records),
        "n_incomplete_trials": n_incomplete_trials,
        "n_harbor_reward_valid_verified": 0,
        "n_scored": 0,
        "n_parsed": 0,
        "n_missing_answer": 0,
        "n_invalid_answer": 0,
        "n_exceptions": 0,
        "n_verifier_faults": 0,
        "n_invalid_reward_trials": 0,
        "n_at_theoretical_max": 0,
        "n_exact_1_0": 0,
        "n_tied_below_1": 0,
    }

    flat_harbor_rewards: list[float] = []
    flat_valid_rhos: list[float] = []
    inconsistency_samples: list[str] = []
    unindexed_set = {id(r) for r in unindexed_records}

    for rec in selected_records:
        v_res = _validate_trial_verifier_record(rec, is_unindexed=(id(rec) in unindexed_set))
        has_exception = v_res["has_exception"]
        is_reward_valid = v_res["is_reward_valid"]
        harbor_reward = v_res["harbor_reward"]
        if has_exception:
            counts["n_exceptions"] += 1
        if not is_reward_valid:
            counts["n_invalid_reward_trials"] += 1

        rec["has_exception"] = has_exception
        rec["is_reward_valid"] = is_reward_valid
        rec["harbor_reward"] = harbor_reward
        rec["parsed_rho"] = v_res["parsed_rho"]
        rec["verifier_ok"] = v_res["verifier_ok"]

        if v_res["status"] == "missing_answer":
            counts["n_missing_answer"] += 1
        elif v_res["status"] == "invalid_answer":
            counts["n_invalid_answer"] += 1

        if v_res["fault_msg"] is not None:
            counts["n_verifier_faults"] += 1
            inconsistency_samples.append(v_res["fault_msg"])
            continue

        if v_res["parsed_rho"] is not None:
            assert harbor_reward is not None
            counts["n_scored"] += 1
            counts["n_parsed"] += 1
            counts["n_harbor_reward_valid_verified"] += 1
            flat_harbor_rewards.append(harbor_reward)
            flat_valid_rhos.append(v_res["parsed_rho"])
            if v_res["at_max"]:
                counts["n_at_theoretical_max"] += 1
            if v_res["exact_1_0"]:
                counts["n_exact_1_0"] += 1
            elif v_res["tied_below_1"]:
                counts["n_tied_below_1"] += 1
        elif v_res["status"] in {"missing_answer", "invalid_answer"} and v_res["verifier_ok"]:
            assert harbor_reward is not None
            counts["n_harbor_reward_valid_verified"] += 1
            flat_harbor_rewards.append(harbor_reward)

    if counts["n_exceptions"] > 0:
        coverage_issues.append(f"{counts['n_exceptions']} trial exception(s)")
    if counts["n_verifier_faults"] > 0:
        detail_suffix = f" ({'; '.join(inconsistency_samples[:3])})" if inconsistency_samples else ""
        coverage_issues.append(f"{counts['n_verifier_faults']} verifier/consistency fault(s){detail_suffix}")
    if counts["n_invalid_reward_trials"] > 0:
        coverage_issues.append(f"{counts['n_invalid_reward_trials']} trial(s) with reward_valid=false")
    if counts["n_missing_answer"] > 0:
        coverage_issues.append(f"{counts['n_missing_answer']} missing answer(s)")
    if counts["n_invalid_answer"] > 0:
        coverage_issues.append(f"{counts['n_invalid_answer']} invalid answer(s)")

    all_cell_keys = set(planned_cells) | set(selected_cells)
    all_assays_in_job = {assay for assay, _seed in all_cell_keys}
    all_seeds_in_job = sorted({seed for _assay, seed in all_cell_keys})

    cell_valid_means_by_seed: dict[int, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    cell_zero_means_by_seed: dict[int, dict[str, float]] = defaultdict(dict)

    for assay, seed in sorted(all_cell_keys):
        cell_trials = selected_cells.get((assay, seed), [])
        planned_count = planned_cells.get((assay, seed), 0)
        denom = max(planned_count, len(cell_trials))
        if denom > 0:
            zero_sum = 0.0
            for rec in cell_trials:
                parsed_rho = rec["parsed_rho"] if rec.get("verifier_ok") else None
                if parsed_rho is not None:
                    cell_valid_means_by_seed[seed][assay].append(float(parsed_rho))
                    zero_sum += float(parsed_rho)
            cell_zero_means_by_seed[seed][assay] = zero_sum / denom

    valid_by_seed_clean = {
        seed: {assay: st.mean(vals) for assay, vals in by_assay.items()}
        for seed, by_assay in sorted(cell_valid_means_by_seed.items())
    }
    if len(all_seeds_in_job) > 1:
        for s in all_seeds_in_job:
            missing_in_seed = all_assays_in_job - set(valid_by_seed_clean.get(s, {}))
            if missing_in_seed:
                coverage_issues.append(
                    f"uneven seed coverage (seed {s}): {len(missing_in_seed)} missing assay(s)"
                )

    all_planned_provenanced = (
        filtered_planned is not None
        and len(filtered_planned) > 0
        and all((p["task_key"], p["task_digest"]) in verified_provenance for p in filtered_planned)
        and all(
            rec.get("provenance") is not None and rec.get("locked_digest") is not None
            for rec in selected_records
        )
    )
    if not all_planned_provenanced and (
        n_planned_trials > 0 or bool(selected_records)
    ):
        msg = "zero-filled nested macro unavailable without verified provenance for every planned trial"
        if msg not in coverage_issues:
            coverage_issues.append(msg)

    upstream_nested_macro_valid, seed_scores_valid = _seed_macro_mean(valid_by_seed_clean, meta)
    if all_planned_provenanced:
        diag_nested_macro_zero, _ = _seed_macro_mean(cell_zero_means_by_seed, meta)
    else:
        diag_nested_macro_zero = None

    if coverage_issues and not allow_incomplete:
        raise ValueError(
            "Incomplete or invalid coverage: "
            + "; ".join(coverage_issues)
            + ". Pass --allow-incomplete for diagnostic partial scoring."
        )

    assay_seed_averaged: dict[str, list[float]] = defaultdict(list)
    for _seed, by_assay in sorted(cell_valid_means_by_seed.items()):
        for assay, vals in by_assay.items():
            assay_seed_averaged[assay].append(st.mean(vals))
    assay_mean_valid = {assay: st.mean(vals) for assay, vals in assay_seed_averaged.items()}

    diag_flat_denom = max(n_planned_trials, len(selected_records) + n_incomplete_trials)
    diag_flat_zero = (sum(flat_valid_rhos) / diag_flat_denom) if diag_flat_denom > 0 else None

    cohort_sample = completed_records[0] if completed_records else {}
    return {
        "job_dir": str(job_path),
        "agent": cohort_sample.get("agent_name"),
        "agent_version": cohort_sample.get("agent_version"),
        "model": cohort_sample.get("model_name"),
        "nominal_size": cohort_sample.get("nominal_size"),
        "complete_coverage": not coverage_issues,
        "diagnostic_only": bool(coverage_issues),
        "coverage_issues": coverage_issues,
        "n_repetitions": n_repetitions,
        "n_retries": parsed_job_stats["retries"] if parsed_job_stats is not None else None,
        "seeds": all_seeds_in_job,
        **counts,
        "harbor_flat_mean_rho": st.mean(flat_harbor_rewards) if flat_harbor_rewards else None,
        "valid_flat_mean_rho": st.mean(flat_valid_rhos) if flat_valid_rhos else None,
        "diagnostic_flat_mean_zero_filled": diag_flat_zero,
        "upstream_nested_macro_valid_only": upstream_nested_macro_valid,
        "upstream_seed_scores": seed_scores_valid,
        "upstream_seed_se": (
            st.stdev(seed_scores_valid.values()) / math.sqrt(len(seed_scores_valid))
            if len(seed_scores_valid) >= 2
            else None
        ),
        "repetition_headlines": None,
        "repetition_mean_rho": None,
        "repetition_sem_rho": None,
        "diagnostic_nested_macro_zero_filled": diag_nested_macro_zero,
        "by_selection_valid_only": nested_macro(assay_mean_valid, meta, "function"),
        "by_taxon_valid_only": nested_macro(assay_mean_valid, meta, "taxon"),
        "by_msa_category_valid_only": nested_macro(assay_mean_valid, meta, "msa_category"),
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compute upstream ProteinGym UniProt-nested macro and Harbor flat metrics for a PG-LLM job."
    )
    parser.add_argument("job_dir", type=Path, help="Path to a Harbor job output directory.")
    parser.add_argument("--agent", type=str, default=None, help="Filter trials by agent name.")
    parser.add_argument("--agent-version", type=str, default=None, help="Filter trials by agent version.")
    parser.add_argument("--model", type=str, default=None, help="Filter trials by model name.")
    parser.add_argument("--size", type=int, default=None, help="Filter trials by nominal variant size.")
    parser.add_argument(
        "--attempt",
        type=int,
        default=None,
        help="Diagnostic 1-based attempt index per (assay, seed) cell (prefer separate jobs for parity repetitions).",
    )
    parser.add_argument(
        "--dataset-dir",
        type=Path,
        default=None,
        help="Optional path to the PG-LLM task dataset directory when scoring relocated job artifacts.",
    )
    parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="Emit diagnostic partial summary instead of failing on incomplete/invalid coverage.",
    )
    parser.add_argument("--json", action="store_true", help="Emit full summary as JSON.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        summary = score_job(
            args.job_dir,
            agent=args.agent,
            agent_version=args.agent_version,
            model=args.model,
            size=args.size,
            attempt=args.attempt,
            dataset_dir=args.dataset_dir,
            allow_incomplete=args.allow_incomplete,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(summary, indent=2))
        return 0

    def _fmt(val: float | None) -> str:
        return f"{val:.12g}" if val is not None else "n/a"

    print(f"PG-LLM Job: {summary['job_dir']}")
    print(
        f"Cohort: agent={summary['agent']}@{summary['agent_version']} "
        f"model={summary['model']} size={summary['nominal_size']} "
        f"| Complete: {summary['complete_coverage']}"
    )
    print(
        f"Trials: planned={summary['n_planned_trials']} observed={summary['n_trials']} "
        f"(incomplete={summary['n_incomplete_trials']}) | "
        f"Parsed: {summary['n_parsed']} | Missing: {summary['n_missing_answer']} | "
        f"Invalid: {summary['n_invalid_answer']} | Exceptions: {summary['n_exceptions']} | "
        f"Verifier faults: {summary['n_verifier_faults']} | Invalid rewards: {summary['n_invalid_reward_trials']} | "
        f"Retries: {summary['n_retries']}"
    )
    print(
        f"Theoretical max: {summary['n_at_theoretical_max']}/{summary['n_parsed']} "
        f"(exact_1.0={summary['n_exact_1_0']}, tied_below_1={summary['n_tied_below_1']})"
    )
    print(
        f"Harbor flat mean (verified reward_valid "
        f"{summary['n_harbor_reward_valid_verified']}/{summary['n_planned_trials']}): "
        f"{_fmt(summary['harbor_flat_mean_rho'])}"
    )
    print(
        f"Diagnostic flat mean (0-filled over {summary['n_planned_trials']} planned): "
        f"{_fmt(summary['diagnostic_flat_mean_zero_filled'])}"
    )
    print(f"Upstream nested macro (valid-only): {_fmt(summary['upstream_nested_macro_valid_only'])}")
    print(f"Diagnostic nested macro (0-filled): {_fmt(summary['diagnostic_nested_macro_zero_filled'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
