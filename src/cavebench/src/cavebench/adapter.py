"""Generate Harbor tasks from the CaveBench test split.

CaveBench (PIER-Lab/CaveBench, arXiv:2611.32251) asks a vision-language model to
infer the hidden external control events applied to a simulated apparatus from
interior frames F0..Fk. One Harbor task is one upstream test question
(1,180 questions = 200 episodes asked at k = 1..6; apparatus S11 only at k = 3..6).

The adapter only needs the public question file and the labels. Frames are not
copied into the task: each task's Dockerfile downloads the pinned, SHA-256
checked test-frame archive of its apparatus from Hugging Face at build time and
keeps only that question's frames.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import urllib.request
from collections import defaultdict
from pathlib import Path

HF_REPO = "linguanxin/CaveBench"
HF_REVISION = "df3c19c4cf2fe0ff08aef1d5e47f7ac7a8f22733"
UPSTREAM_REPO = "https://github.com/PIER-Lab/CaveBench"
UPSTREAM_COMMIT = "c0f8cbee47af72a9437b5ab82f3f03b391e2a819"

# Pinned inputs from the HF dataset at HF_REVISION.
SOURCE_FILES = {
    "questions.jsonl": "2ceee08f5bfe89e9bd68ce36863dc369708cc017d2e09c137fe4142e62cdc52c",
    "labels.jsonl": "bcfd27a127e01be3ab7601278cd2025cee8e6011eced307675e33f8dab0f4713",
    "test_frames_manifest.json": "b53b091ac20b8aafc8fa93ec509be57ad88b7028a5d4673c3828312b962fb30d",
}
# tests/contracts.py is upstream contracts.py at UPSTREAM_COMMIT, unmodified.
CONTRACTS_SHA256 = "c5562a968209df435a87e9b21fa73c5fdce46bf54707a0ce14e1490151438415"

PUBLIC_QUESTION_FIELDS = {
    "id", "scene", "paper_scene", "domain", "title", "sample_id", "k",
    "prompt", "images", "allowed_labels", "candidates",
}

DATASET_ORG = "cavebench"
EXPECTED_TASKS = 1180
# Tasks that cannot reliably reach reward 1.0 with the oracle, with reasons.
# None: the oracle writes the simulator-log reference, which the upstream scorer
# accepts for all 1,180 questions.
EXCLUDED_TASKS: dict[str, str] = {}

# Parity subset: one question per (apparatus, k), 118 = 19 x 6 + S11 x 4 (10%).
# Within each stratum the episode with the smallest sha256(PARITY_SEED/id) wins.
PARITY_SEED = "cavebench-parity-v1"

TEMPLATE_DIR = Path(__file__).parent / "task-template"
CACHE_DIR = Path.home() / ".cache" / "harbor-cavebench" / HF_REVISION
PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def render(template: str, values: dict[str, object]) -> str:
    """Single-pass {key} substitution; unknown braces (JSON, F{i}) are kept."""
    return PLACEHOLDER.sub(
        lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0),
        template,
    )


def toml_str(value: str) -> str:
    """Escape a value for a TOML basic string (JSON escapes are valid TOML)."""
    return json.dumps(value, ensure_ascii=False)[1:-1]


def task_dir_name(question: dict) -> str:
    """scene_01/sample_0001/k2 in paper apparatus S01 -> s01-sample-0001-k2."""
    raw = f"{question['paper_scene']}-{question['sample_id']}-k{question['k']}"
    return re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")


def parity_ids(questions: list[dict]) -> set[str]:
    strata: dict[tuple[str, int], list[str]] = defaultdict(list)
    for q in questions:
        strata[(q["paper_scene"], q["k"])].append(q["id"])
    return {
        min(ids, key=lambda i: hashlib.sha256(f"{PARITY_SEED}/{i}".encode()).hexdigest())
        for ids in strata.values()
    }


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class CaveBenchAdapter:
    def __init__(
        self,
        output_dir: Path,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
        split: str = "full",
        source_dir: Path | None = None,
        **kwargs,
    ):
        """
        Args:
            output_dir: directory to write the generated tasks to.
            limit: generate only the first N tasks (after split/task-id filtering).
            overwrite: replace existing task directories.
            task_ids: only these tasks; accepts Harbor names (s01-sample-0001-k2)
                or upstream IDs (scene_01/sample_0001/k2).
            split: "full" (1,180 tasks) or "parity" (118-task subset).
            source_dir: local copy of the pinned HF files (questions.jsonl,
                labels.jsonl, test_frames_manifest.json); default: download them.
        """
        if split not in ("full", "parity"):
            raise ValueError(f"unknown split {split!r}; use 'full' or 'parity'")
        self.output_dir = Path(output_dir)
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = set(task_ids) if task_ids else None
        self.split = split
        self.source_dir = Path(source_dir) if source_dir else None

    # ------------------------------------------------------------------ inputs
    def _source_file(self, name: str) -> Path:
        if self.source_dir is not None:
            path = self.source_dir / name
        else:
            path = CACHE_DIR / name
            if not path.is_file():
                path.parent.mkdir(parents=True, exist_ok=True)
                url = f"https://huggingface.co/datasets/{HF_REPO}/resolve/{HF_REVISION}/{name}"
                tmp = path.with_name(path.name + ".part")
                with urllib.request.urlopen(url, timeout=120) as src, tmp.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                tmp.replace(path)
        if sha256_file(path) != SOURCE_FILES[name]:
            raise ValueError(f"{path} does not match the pinned SHA-256 for {name}")
        return path

    def load(self) -> tuple[list[dict], dict[str, list], dict[str, dict]]:
        questions = read_jsonl(self._source_file("questions.jsonl"))
        labels = {r["id"]: r["operations"] for r in read_jsonl(self._source_file("labels.jsonl"))}
        archives = {
            a["scene"]: a
            for a in json.loads(self._source_file("test_frames_manifest.json").read_text(encoding="utf-8"))
        }
        if len(questions) != EXPECTED_TASKS or set(labels) != {q["id"] for q in questions}:
            raise ValueError("question/label sets do not match the pinned release")
        return questions, labels, archives

    # -------------------------------------------------------------- generation
    def _selected(self, questions: list[dict]) -> list[dict]:
        chosen = [q for q in questions if q["id"] not in EXCLUDED_TASKS]
        if self.split == "parity":
            keep = parity_ids(questions)
            chosen = [q for q in chosen if q["id"] in keep]
        if self.task_ids is not None:
            chosen = [q for q in chosen if q["id"] in self.task_ids or task_dir_name(q) in self.task_ids]
        chosen.sort(key=lambda q: (q["paper_scene"], q["sample_id"], q["k"]))
        return chosen[: self.limit] if self.limit is not None else chosen

    def _write_task(self, q: dict, reference: list, archive: dict) -> None:
        name = task_dir_name(q)
        out = self.output_dir / name
        if out.exists():
            if not self.overwrite:
                return
            shutil.rmtree(out)
        shutil.copytree(TEMPLATE_DIR, out, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

        k = q["k"]
        expected_images = [f"media/{q['paper_scene']}/{q['sample_id']}/F{i}.png" for i in range(k + 1)]
        if q["images"] != expected_images:
            raise ValueError(f"unexpected image list for {q['id']}")
        values = {
            "task_name": f"{DATASET_ORG}/{name}",
            "description": toml_str(
                f"CaveBench {q['paper_scene']} ({q['title']}, {q['domain']}): infer {k} hidden "
                f"control event(s) from interior frames F0-F{k}."
            ),
            "upstream_id": toml_str(q["id"]),
            "paper_scene": q["paper_scene"],
            "scene_tag": q["paper_scene"].lower(),
            "title": toml_str(q["title"]),
            "domain": toml_str(q["domain"]),
            "domain_tag": re.sub(r"[^a-z0-9]+", "-", q["domain"].lower()).strip("-"),
            "sample_id": q["sample_id"],
            "k": k,
            "source_commit": UPSTREAM_COMMIT,
            "hf_revision": HF_REVISION,
            "archive_sha256": archive["sha256"],
            "archive_bytes": archive["bytes"],
            "frame_copies": "\n".join(
                f"COPY --from=frames /opt/cavebench/{img} /workspace/frames/F{i}.png"
                for i, img in enumerate(q["images"])
            ),
        }
        for rel in ("task.toml", "environment/Dockerfile"):
            path = out / rel
            path.write_text(render(path.read_text(encoding="utf-8"), values), encoding="utf-8")
        instruction = out / "instruction.md"
        instruction.write_text(
            render(instruction.read_text(encoding="utf-8"), {"k": k, "prompt": q["prompt"]}),
            encoding="utf-8",
        )

        # The upstream question row holds public inputs only (no labels). The
        # parity agent rebuilds the upstream request from it.
        if set(q) != PUBLIC_QUESTION_FIELDS:
            raise ValueError(f"unexpected question fields for {q['id']}: {sorted(q)}")
        public = {**q, "frames": [f"/workspace/frames/F{i}.png" for i in range(k + 1)]}
        (out / "environment" / "question.json").write_text(
            json.dumps(public, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (out / "solution" / "answer.json").write_text(
            json.dumps({"operations": reference}, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        expected = {
            "id": q["id"],
            "k": k,
            "allowed_labels": q["allowed_labels"],
            "candidates": q["candidates"],
            "operations": reference,
        }
        (out / "tests" / "expected.json").write_text(
            json.dumps(expected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def run(self) -> None:
        if sha256_file(TEMPLATE_DIR / "tests" / "contracts.py") != CONTRACTS_SHA256:
            raise ValueError("tests/contracts.py differs from upstream contracts.py")
        questions, labels, archives = self.load()
        selected = self._selected(questions)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        for q in selected:
            self._write_task(q, labels[q["id"]], archives[q["paper_scene"]])
        print(f"Wrote {len(selected)} CaveBench tasks ({self.split} split) to {self.output_dir}")
