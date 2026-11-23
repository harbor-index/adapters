"""Deterministic Spearman rank correlation scorer for PG-LLM Harbor tasks.

Adapted from ``rohitarorayyc/proteingym-llm`` (MIT License).
Copyright (c) 2026 Rohit Arora, Leo Tianlai Chen, Melissa Du, Debora S. Marks,
and George M. Church.

Matches the upstream ProteinGym-LLM ``parse_ranking`` and ``score_ranking``
implementation in pure Python stdlib while limiting answer reads and keeping
verifier infrastructure faults distinct from submission failures.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

_MAX_ANSWER_BYTES = 16 * 1024 * 1024


def _read_bounded_text(path: Path, max_bytes: int = _MAX_ANSWER_BYTES) -> str:
    stat = path.stat()
    if stat.st_size > max_bytes:
        raise ValueError(f"answer file exceeds {max_bytes} byte limit")
    with path.open("rb") as handle:
        raw = handle.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError(f"answer file exceeds {max_bytes} byte limit")
    return raw.decode("utf-8")


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def parse_ranking(text: str, ids: list[str]) -> list[str] | None:
    """Extract the JSON ranking; returns ordered ID list (best->worst) or None."""
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
    # Guard against e.g. M1000 matching M100.
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


def spearman(a: list[float], b: list[float]) -> float:
    ra, rb = _rankdata(a), _rankdata(b)
    n = len(a)
    if n < 2:
        return 0.0
    ma, mb = sum(ra) / n, sum(rb) / n
    cov = sum((ra[i] - ma) * (rb[i] - mb) for i in range(n))
    va = math.sqrt(sum((x - ma) ** 2 for x in ra))
    vb = math.sqrt(sum((x - mb) ** 2 for x in rb))
    return cov / (va * vb) if va and vb else 0.0


def score_ranking(ranking: list[str], ids: list[str], dms_scores: list[float]) -> float:
    """Spearman correlation between predicted ranking and true DMS scores."""
    pos = {mid: i for i, mid in enumerate(ranking)}
    model_pred = [-pos[mid] for mid in ids]
    return spearman(model_pred, dms_scores)


_CANONICAL_ID_RE = re.compile(r"^M\d{2,3}$")


def _is_canonical_id(mid: object) -> bool:
    if not isinstance(mid, str) or not _CANONICAL_ID_RE.fullmatch(mid):
        return False
    num = int(mid[1:])
    return num >= 1 and f"M{num:02d}" == mid


def _load_labels(labels_path: Path) -> tuple[dict[str, object], list[str], list[float]]:
    labels_data = json.loads(labels_path.read_text(encoding="utf-8"))
    if not isinstance(labels_data, dict):
        raise ValueError("labels.json must be a JSON object")

    raw_ids = labels_data.get("ids")
    raw_variants = labels_data.get("variants")
    if not isinstance(raw_ids, list) or len(raw_ids) < 2:
        raise ValueError("labels.json 'ids' must be a list of at least 2 variant IDs")
    if not isinstance(raw_variants, list) or len(raw_variants) != len(raw_ids):
        raise ValueError("labels.json 'variants' length does not match 'ids'")

    ids: list[str] = []
    seen_ids: set[str] = set()
    for mid in raw_ids:
        if not _is_canonical_id(mid):
            raise ValueError(f"non-canonical variant id in labels.json 'ids': {mid!r}")
        assert isinstance(mid, str)
        if mid in seen_ids:
            raise ValueError(f"duplicate variant id in labels.json 'ids': {mid!r}")
        seen_ids.add(mid)
        ids.append(mid)

    score_by_id: dict[str, float] = {}
    for item in raw_variants:
        if not isinstance(item, dict):
            raise ValueError("each entry in labels.json 'variants' must be a JSON object")
        vid = item.get("id")
        if not _is_canonical_id(vid):
            raise ValueError(f"non-canonical variant id in labels.json 'variants': {vid!r}")
        assert isinstance(vid, str)
        if vid in score_by_id:
            raise ValueError(f"duplicate variant id in labels.json 'variants': {vid!r}")
        raw_score = item.get("dms_score")
        if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
            raise ValueError(f"non-numeric dms_score for {vid!r}: {raw_score!r}")
        try:
            score_f = float(raw_score)
        except OverflowError as exc:
            raise ValueError(f"overflowing dms_score for {vid!r}: {raw_score!r}") from exc
        if not math.isfinite(score_f):
            raise ValueError(f"non-finite dms_score for {vid!r}: {raw_score!r}")
        score_by_id[vid] = score_f

    if set(score_by_id) != seen_ids:
        missing = sorted(seen_ids - set(score_by_id))
        extra = sorted(set(score_by_id) - seen_ids)
        raise ValueError(
            f"labels.json variant IDs do not match 'ids' (missing={missing}, extra={extra})"
        )

    dms_scores = [score_by_id[mid] for mid in ids]
    return labels_data, ids, dms_scores


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        print(
            "Usage: grade.py <answer_path> <labels_path> <reward_path> <details_path>",
            file=sys.stderr,
        )
        return 2

    answer_path = Path(argv[1])
    labels_path = Path(argv[2])
    reward_path = Path(argv[3])
    details_path = Path(argv[4])

    labels_data, ids, dms_scores = _load_labels(labels_path)
    oracle_indices = sorted(range(len(ids)), key=lambda idx: (-dms_scores[idx], ids[idx]))
    oracle_ranking = [ids[idx] for idx in oracle_indices]
    max_rho = score_ranking(oracle_ranking, ids, dms_scores)

    details: dict[str, object] = {
        "assay": labels_data.get("assay"),
        "nominal_size": labels_data.get("nominal_size", labels_data.get("size")),
        "size": labels_data.get("size"),
        "seed": labels_data.get("seed"),
        "uniprot_id": labels_data.get("uniprot_id"),
        "selection_type": labels_data.get("selection_type"),
        "taxon": labels_data.get("taxon"),
        "msa_category": labels_data.get("msa_category"),
        "n_variants": len(ids),
        "status": "missing_answer",
        "parsed": False,
        "spearman_rho": None,
        "max_possible_spearman_rho": max_rho if math.isfinite(max_rho) else None,
        "at_theoretical_max": False,
        "reward": 0.0,
        "error": None,
    }

    reward = 0.0
    try:
        st_ans = answer_path.stat()
    except FileNotFoundError:
        details["status"] = "missing_answer"
        details["error"] = f"missing answer file: {answer_path}"
    else:
        if not stat.S_ISREG(st_ans.st_mode):
            details["status"] = "missing_answer"
            details["error"] = f"missing answer file: {answer_path}"
        else:
            try:
                raw_text = _read_bounded_text(answer_path)
            except FileNotFoundError:
                details["status"] = "missing_answer"
                details["error"] = f"missing answer file: {answer_path}"
            except (UnicodeDecodeError, ValueError) as exc:
                details["status"] = "invalid_answer"
                details["error"] = f"failed to read answer file: {exc}"
            else:
                ranking = parse_ranking(raw_text, ids)
                if ranking is None:
                    details["status"] = "invalid_answer"
                    details["error"] = "complete ranking not found"
                else:
                    rho = score_ranking(ranking, ids, dms_scores)
                    if not math.isfinite(rho):
                        details["status"] = "invalid_answer"
                        details["error"] = f"non-finite spearman score: {rho!r}"
                    else:
                        reward = rho
                        details["status"] = "scored"
                        details["parsed"] = True
                        details["spearman_rho"] = rho
                        details["at_theoretical_max"] = math.isfinite(max_rho) and abs(rho - max_rho) < 1e-12
                        details["reward"] = rho

    _atomic_write_text(details_path, json.dumps(details, indent=2) + "\n")
    _atomic_write_text(reward_path, f"{reward:.12g}\n")
    print(json.dumps(details))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
