"""Harbor adapter for MotifBench (Critic's Eye) long-document motif recovery.

Source dataset: https://huggingface.co/datasets/Acture/MotifBench
Paper: Critic's Eye: Review-Grounded Graph Reasoning for Long-Form Motif Discovery (EMNLP 2026 Findings)
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "task-template"
DEFAULT_HF_REPO = "Acture/MotifBench"
DEFAULT_HF_REVISION = "12fadc6a46aa8d2fcc7848777574122afeccf59f"

REQUIRED_FILES = (
    "macbeth_full.txt",
    "macbeth_full.pdf",
    "macbeth_clean.jsonl",
    "motifs_clean.json",
)

Granularity = Literal["scene-group", "act", "query"]
SplitMode = Literal["full", "parity"]

# Contiguous Act-respecting scene groups covering all 270 cleaned Macbeth query lines
# across 12 balanced tasks (13-36 queries/task; never splits a scene or crosses Acts).
SCENE_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("macbeth-act1-s1-s2", "Act 1, Scenes 1–2", ("act_1_scene_1", "act_1_scene_2")),
    ("macbeth-act1-s3", "Act 1, Scene 3", ("act_1_scene_3",)),
    ("macbeth-act1-s4-s5", "Act 1, Scenes 4–5", ("act_1_scene_4", "act_1_scene_5")),
    ("macbeth-act1-s6-s7", "Act 1, Scenes 6–7", ("act_1_scene_6", "act_1_scene_7")),
    ("macbeth-act2-s1-s2", "Act 2, Scenes 1–2", ("act_2_scene_1", "act_2_scene_2")),
    ("macbeth-act2-s3-s4", "Act 2, Scenes 3–4", ("act_2_scene_3", "act_2_scene_4")),
    ("macbeth-act3-s1-s2", "Act 3, Scenes 1–2", ("act_3_scene_1", "act_3_scene_2")),
    (
        "macbeth-act3-s4-s6",
        "Act 3, Scenes 4–6",
        ("act_3_scene_4", "act_3_scene_5", "act_3_scene_6"),
    ),
    ("macbeth-act4-s1-s2", "Act 4, Scenes 1–2", ("act_4_scene_1", "act_4_scene_2")),
    ("macbeth-act4-s3", "Act 4, Scene 3", ("act_4_scene_3",)),
    (
        "macbeth-act5-s1-s3",
        "Act 5, Scenes 1–3",
        ("act_5_scene_1", "act_5_scene_2", "act_5_scene_3"),
    ),
    (
        "macbeth-act5-s4-s8",
        "Act 5, Scenes 4–8",
        (
            "act_5_scene_4",
            "act_5_scene_5",
            "act_5_scene_6",
            "act_5_scene_7",
            "act_5_scene_8",
        ),
    ),
)

ACT_GROUPS: tuple[tuple[str, str, str], ...] = (
    ("macbeth-act1", "Act 1", "act_1"),
    ("macbeth-act2", "Act 2", "act_2"),
    ("macbeth-act3", "Act 3", "act_3"),
    ("macbeth-act4", "Act 4", "act_4"),
    ("macbeth-act5", "Act 5", "act_5"),
)


@dataclass(frozen=True)
class QueryRecord:
    line_id: str
    line_index: int
    citation: str
    act_id: str
    scene_id: str
    speaker: str
    text: str
    gold_motif_ids: tuple[str, ...]

    def to_agent_dict(self) -> dict[str, object]:
        parts = self.citation.split(".")
        return {
            "line_id": self.line_id,
            "citation": self.citation,
            "act": int(parts[0]),
            "scene": int(parts[1]),
            "line_number": int(parts[2]),
            "speaker": self.speaker,
            "text": self.text,
        }

    def to_gt_dict(self) -> dict[str, object]:
        return {
            "line_id": self.line_id,
            "citation": self.citation,
            "act_id": self.act_id,
            "scene_id": self.scene_id,
            "gold_motif_ids": list(self.gold_motif_ids),
        }


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    title: str
    description: str
    queries: tuple[QueryRecord, ...]
    agent_timeout_sec: float


def _make_executable(path: Path) -> None:
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class MotifBenchAdapter:
    """Converts cleaned MotifBench Macbeth records into Harbor task directories."""

    NAME = "motifbench"

    def __init__(
        self,
        output_dir: Path,
        *,
        data_dir: Path | None = None,
        cache_dir: Path | None = None,
        hf_repo: str = DEFAULT_HF_REPO,
        hf_revision: str = DEFAULT_HF_REVISION,
        granularity: Granularity = "scene-group",
        split: SplitMode = "full",
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.data_dir = Path(data_dir) if data_dir is not None else None
        self.cache_dir = Path(cache_dir) if cache_dir is not None else (
            Path(__file__).resolve().parents[2] / ".cache" / "macbeth"
        )
        self.hf_repo = hf_repo
        self.hf_revision = hf_revision
        self.granularity: Granularity = granularity
        self.split: SplitMode = split
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = task_ids

    def _resolve_assets(self) -> dict[str, Path]:
        if self.data_dir is not None:
            resolved = {name: self.data_dir / name for name in REQUIRED_FILES}
            missing = [str(p) for p in resolved.values() if not p.exists()]
            if missing:
                raise FileNotFoundError(
                    "Missing required MotifBench files in --data-dir: "
                    + ", ".join(missing)
                )
            return resolved

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        resolved = {}
        token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACEHUB_API_TOKEN")
        for name in REQUIRED_FILES:
            dest = self.cache_dir / name
            if not dest.exists():
                url = (
                    f"https://huggingface.co/datasets/{self.hf_repo}/resolve/"
                    f"{self.hf_revision}/macbeth/{name}"
                )
                logger.info("Downloading %s -> %s", url, dest)
                req = urllib.request.Request(url)
                if token is not None and token != "":
                    req.add_header("Authorization", f"Bearer {token}")
                try:
                    with urllib.request.urlopen(req) as resp:
                        dest.write_bytes(resp.read())
                except (urllib.error.URLError, OSError) as exc:
                    raise RuntimeError(
                        f"Failed to download {url}: {exc}. "
                        "Pass --data-dir <path> to use a locally exported MotifBench directory."
                    ) from exc
            resolved[name] = dest
        return resolved

    def _load_records(
        self, assets: dict[str, Path]
    ) -> tuple[list[dict[str, str]], list[QueryRecord]]:
        raw_motifs = json.loads(
            assets["motifs_clean.json"].read_text(encoding="utf-8")
        )
        motifs = cast(list[dict[str, str]], raw_motifs)
        valid_motif_ids = {m["motif_id"] for m in motifs}

        rows = [
            json.loads(line)
            for line in assets["macbeth_clean.jsonl"].read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

        grouped_golds: dict[str, list[str]] = defaultdict(list)
        meta_by_line: dict[str, dict[str, object]] = {}
        for row in rows:
            lid = str(row["node_id"])
            mid = str(row["motif_id"])
            if mid not in valid_motif_ids:
                continue
            if mid not in grouped_golds[lid]:
                grouped_golds[lid].append(mid)
            if lid not in meta_by_line:
                meta_by_line[lid] = row

        records: list[QueryRecord] = []
        for lid, gold_ids in grouped_golds.items():
            meta = meta_by_line[lid]
            gold_sorted = tuple(sorted(gold_ids, key=lambda m: int(m.split("_")[1])))
            raw_line_index = meta["line_index"]
            line_index = raw_line_index if isinstance(raw_line_index, int) else int(str(raw_line_index))
            records.append(
                QueryRecord(
                    line_id=lid,
                    line_index=line_index,
                    citation=str(meta["citation"]),
                    act_id=str(meta["act_id"]),
                    scene_id=str(meta["scene_id"]),
                    speaker=str(meta.get("speaker_name") or meta.get("speaker") or "UNKNOWN"),
                    text=str(meta["text"]),
                    gold_motif_ids=gold_sorted,
                )
            )
        records.sort(key=lambda r: r.line_index)
        return motifs, records

    def _build_task_specs(self, records: list[QueryRecord]) -> list[TaskSpec]:
        if self.granularity == "scene-group":
            by_scene: dict[str, list[QueryRecord]] = defaultdict(list)
            for rec in records:
                by_scene[rec.scene_id].append(rec)

            specs: list[TaskSpec] = []
            for task_id, label, scene_ids in SCENE_GROUPS:
                q_list: list[QueryRecord] = []
                for sid in scene_ids:
                    q_list.extend(by_scene.get(sid, []))
                q_list.sort(key=lambda r: r.line_index)
                if not q_list:
                    continue
                specs.append(
                    TaskSpec(
                        task_id=task_id,
                        title=f"Macbeth — {label}",
                        description=(
                            f"Rank recurring literary motifs for {len(q_list)} query lines "
                            f"from Macbeth ({label}) using the complete play."
                        ),
                        queries=tuple(q_list),
                        agent_timeout_sec=1800.0,
                    )
                )
            return specs

        if self.granularity == "act":
            by_act: dict[str, list[QueryRecord]] = defaultdict(list)
            for rec in records:
                by_act[rec.act_id].append(rec)

            specs = []
            for task_id, label, act_id in ACT_GROUPS:
                q_list = sorted(by_act.get(act_id, []), key=lambda r: r.line_index)
                if not q_list:
                    continue
                specs.append(
                    TaskSpec(
                        task_id=task_id,
                        title=f"Macbeth — {label}",
                        description=(
                            f"Rank recurring literary motifs for {len(q_list)} query lines "
                            f"from Macbeth ({label}) using the complete play."
                        ),
                        queries=tuple(q_list),
                        agent_timeout_sec=3600.0,
                    )
                )
            return specs

        # granularity == "query": one task per query line
        specs = []
        for rec in records:
            task_id = f"macbeth-{rec.line_id}"
            specs.append(
                TaskSpec(
                    task_id=task_id,
                    title=f"Macbeth {rec.citation} ({rec.line_id})",
                    description=(
                        f"Rank recurring literary motifs for Macbeth {rec.citation} "
                        f"({rec.line_id}) using the complete play."
                    ),
                    queries=(rec,),
                    agent_timeout_sec=600.0,
                )
            )
        if self.split == "parity":
            # Deterministic stratified sample of 50 queries (10 per Act) for single-query parity.
            by_act_q: dict[str, list[TaskSpec]] = defaultdict(list)
            for spec in specs:
                by_act_q[spec.queries[0].act_id].append(spec)
            sampled: list[TaskSpec] = []
            for _, _, act_id in ACT_GROUPS:
                act_specs = by_act_q.get(act_id, [])
                if len(act_specs) <= 10:
                    sampled.extend(act_specs)
                else:
                    step = len(act_specs) / 10.0
                    sampled.extend(act_specs[int(i * step)] for i in range(10))
            return sampled
        return specs

    def _select(self, specs: list[TaskSpec]) -> list[TaskSpec]:
        selected = specs
        if self.task_ids is not None and len(self.task_ids) > 0:
            wanted = {t.lower() for t in self.task_ids}
            selected = [s for s in selected if s.task_id.lower() in wanted]
        if self.limit is not None:
            selected = selected[: max(0, self.limit)]
        return selected

    def _prepare_task(
        self,
        spec: TaskSpec,
        motifs: list[dict[str, str]],
        assets: dict[str, Path],
        task_dir: Path,
    ) -> None:
        if task_dir.exists():
            if not self.overwrite:
                logger.info("Skipping existing task %s (use --overwrite)", task_dir.name)
                return
            shutil.rmtree(task_dir)

        task_dir.mkdir(parents=True, exist_ok=True)
        env_dir = task_dir / "environment"
        sol_dir = task_dir / "solution"
        tests_dir = task_dir / "tests"
        env_dir.mkdir()
        sol_dir.mkdir()
        tests_dir.mkdir()

        # 1. environment/: Dockerfile + full play (.txt & .pdf) + motifs.json + queries.json
        shutil.copy2(TEMPLATE_DIR / "environment/Dockerfile", env_dir / "Dockerfile")
        shutil.copy2(assets["macbeth_full.txt"], env_dir / "macbeth.txt")
        shutil.copy2(assets["macbeth_full.pdf"], env_dir / "macbeth.pdf")
        (env_dir / "motifs.json").write_text(
            json.dumps(motifs, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (env_dir / "queries.json").write_text(
            json.dumps(
                [q.to_agent_dict() for q in spec.queries],
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        # 2. task.toml
        toml_tmpl = (TEMPLATE_DIR / "task.toml").read_text(encoding="utf-8")
        (task_dir / "task.toml").write_text(
            toml_tmpl.format(
                task_id=spec.task_id,
                description=spec.description.replace('"', '\\"'),
                agent_timeout_sec=f"{spec.agent_timeout_sec:.1f}",
            ),
            encoding="utf-8",
        )

        # 3. instruction.md
        query_md = "\n".join(
            f"- `{q.line_id}` (**{q.citation}**, {q.speaker}): \"{q.text}\""
            for q in spec.queries
        )
        instr_tmpl = (TEMPLATE_DIR / "instruction.md").read_text(encoding="utf-8")
        (task_dir / "instruction.md").write_text(
            instr_tmpl.format(
                task_title=spec.title,
                num_motifs=len(motifs),
                num_queries=len(spec.queries),
                timeout_minutes=int(spec.agent_timeout_sec // 60),
                query_list_markdown=query_md,
            ),
            encoding="utf-8",
        )

        # 4. solution/solve.sh (deterministic 100% oracle)
        oracle_preds = {q.line_id: list(q.gold_motif_ids) for q in spec.queries}
        solve_tmpl = (TEMPLATE_DIR / "solution/solve.sh").read_text(encoding="utf-8")
        solve_path = sol_dir / "solve.sh"
        solve_path.write_text(
            solve_tmpl.format(
                oracle_predictions_json=json.dumps(oracle_preds, indent=2)
            ),
            encoding="utf-8",
        )
        _make_executable(solve_path)

        # 5. tests/: separate verifier image + ground_truth.json
        shutil.copy2(TEMPLATE_DIR / "tests/Dockerfile", tests_dir / "Dockerfile")
        test_sh = tests_dir / "test.sh"
        shutil.copy2(TEMPLATE_DIR / "tests/test.sh", test_sh)
        _make_executable(test_sh)
        shutil.copy2(TEMPLATE_DIR / "tests/verifier.py", tests_dir / "verifier.py")
        gt_payload = {
            "task_id": spec.task_id,
            "valid_motif_ids": [m["motif_id"] for m in motifs],
            "queries": [q.to_gt_dict() for q in spec.queries],
        }
        (tests_dir / "ground_truth.json").write_text(
            json.dumps(gt_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def run(self) -> list[Path]:
        assets = self._resolve_assets()
        motifs, records = self._load_records(assets)
        specs = self._select(self._build_task_specs(records))

        self.output_dir.mkdir(parents=True, exist_ok=True)
        generated: list[Path] = []
        for spec in specs:
            task_dir = self.output_dir / spec.task_id
            self._prepare_task(spec, motifs, assets, task_dir)
            generated.append(task_dir)

        logger.info(
            "Generated %d MotifBench Harbor tasks (%s, %d total queries, %d motifs) in %s",
            len(generated),
            self.granularity,
            sum(len(s.queries) for s in specs),
            len(motifs),
            self.output_dir,
        )
        return generated
