"""Adapter that converts MedXpertQA-MM into Harbor tasks.

MedXpertQA (Zuo et al., ICML 2025) is an expert-level medical QA benchmark.
This adapter covers the multimodal ("MM") test split: 2,000 five-option
multiple-choice questions, each paired with one or more clinical images.

Source data (https://huggingface.co/datasets/TsinghuaC3I/MedXpertQA):
    MM/test.jsonl   one question per line
    images.zip      every image referenced by the ``images`` field

Each generated task ships the question images inside the container, asks the
agent to write its answer letter to ``/app/answer.txt`` and grades that file
with the original benchmark's answer-cleansing and exact-match logic.
"""

from __future__ import annotations

import json
import logging
import random
import re
import shutil
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

logger = logging.getLogger(__name__)

HF_REPO_ID = "TsinghuaC3I/MedXpertQA"
HF_QUESTIONS_FILE = "MM/test.jsonl"
HF_IMAGES_FILE = "images.zip"
# Dataset commit the adapter was verified against (2,000 MM test questions, 2,852 images).
HF_REVISION = "7e7c465a68eb2b866926bfa59c8c9d17a8daba65"

# Parity subset: 10% of every (medical_task, question_type) stratum.
PARITY_SAMPLE_RATE = 0.1
PARITY_SEED = 42

TEMPLATE_DIR = Path(__file__).parent / "task-template"

# Candidate locations of the question file inside a local --source-dir.
_LOCAL_QUESTION_FILES = (
    "MM/test.jsonl",
    "test.jsonl",
    "medxpertqa_mm_input.jsonl",
)


@dataclass(frozen=True)
class MedXpertQARecord:
    """One MedXpertQA-MM question in a normalized form."""

    source_id: str  # upstream id, e.g. "MM-26"
    question: str  # question text with the inline "Answer Choices: (A) ..." block
    options: dict[str, str]  # letter -> option text, in upstream order
    label: str  # correct letter
    images: list[str]  # image file names, in upstream order
    medical_task: str
    body_system: str
    question_type: str

    @property
    def task_id(self) -> str:
        """Registry-safe, stable task id derived from the upstream id."""
        return make_task_id(self.source_id)


def make_task_id(source_id: str) -> str:
    task_id = re.sub(r"[^a-z0-9]+", "-", source_id.lower()).strip("-")
    if not task_id:
        raise ValueError(f"Cannot derive a task id from {source_id!r}")
    return task_id


def _source_order(record: MedXpertQARecord) -> tuple[int, str]:
    """Sort key: numeric suffix of the upstream id (MM-0, MM-1, ..., MM-1999)."""
    match = re.search(r"(\d+)$", record.source_id)
    return (int(match.group(1)) if match else -1, record.source_id)


def normalize_record(raw: dict) -> MedXpertQARecord:
    """Accept both the HuggingFace schema and the upstream eval-repo schema.

    HuggingFace: options = {"A": "...", ...}, label = "A", images = ["x.jpeg"]
    Eval repo:   options = [{"letter": "A", "content": "..."}], label = ["A"],
                 images = [{"image_path": "x.jpeg"}]
    """
    options_raw = raw["options"]
    if isinstance(options_raw, dict):
        options = {str(k): str(v) for k, v in options_raw.items()}
    else:
        options = {str(o["letter"]): str(o["content"]) for o in options_raw}

    label_raw = raw["label"]
    if isinstance(label_raw, list):
        if len(label_raw) != 1:
            raise ValueError(f"{raw['id']}: expected one label, got {label_raw}")
        label_raw = label_raw[0]
    label = str(label_raw).strip()
    if label not in options:
        raise ValueError(f"{raw['id']}: label {label!r} is not one of {list(options)}")

    expected_letters = [chr(65 + i) for i in range(len(options))]
    if list(options) != expected_letters:
        raise ValueError(f"{raw['id']}: unexpected option letters {list(options)}")

    images = [
        img["image_path"] if isinstance(img, dict) else str(img)
        for img in raw.get("images", [])
    ]
    if not images:
        raise ValueError(f"{raw['id']}: MM question has no images")

    return MedXpertQARecord(
        source_id=str(raw["id"]),
        question=str(raw["question"]),
        options=options,
        label=label,
        images=images,
        medical_task=str(raw.get("medical_task", "")),
        body_system=str(raw.get("body_system", "")),
        question_type=str(raw.get("question_type", "")),
    )


def select_parity_subset(records: list[MedXpertQARecord]) -> list[MedXpertQARecord]:
    """Deterministic 10% sample, stratified by (medical_task, question_type)."""
    strata: dict[tuple[str, str], list[MedXpertQARecord]] = defaultdict(list)
    for record in sorted(records, key=_source_order):
        strata[(record.medical_task, record.question_type)].append(record)

    rng = random.Random(PARITY_SEED)
    selected: list[MedXpertQARecord] = []
    for key in sorted(strata):
        group = strata[key]
        n = max(1, round(len(group) * PARITY_SAMPLE_RATE))
        selected.extend(rng.sample(group, n))
    return sorted(selected, key=_source_order)


class ImageStore:
    """Resolves image file names against a directory or the upstream images.zip."""

    def __init__(self, directory: Path | None = None, archive: Path | None = None):
        self._zip: zipfile.ZipFile | None = None
        self._dir_index: dict[str, Path] = {}
        self._zip_index: dict[str, str] = {}

        if directory is not None:
            for path in directory.rglob("*"):
                if path.is_file():
                    self._dir_index.setdefault(path.name, path)
        if archive is not None:
            self._zip = zipfile.ZipFile(archive)
            for name in self._zip.namelist():
                if not name.endswith("/"):
                    self._zip_index.setdefault(PurePosixPath(name).name, name)

        if not self._dir_index and not self._zip_index:
            raise FileNotFoundError("No MedXpertQA images found (directory or zip)")

    def write(self, image_name: str, destination: Path) -> None:
        if image_name in self._dir_index:
            shutil.copyfile(self._dir_index[image_name], destination)
        elif image_name in self._zip_index and self._zip is not None:
            destination.write_bytes(self._zip.read(self._zip_index[image_name]))
        else:
            raise FileNotFoundError(f"Image not found in source data: {image_name}")

    def close(self) -> None:
        if self._zip is not None:
            self._zip.close()


class MedXpertQAMMAdapter:
    def __init__(
        self,
        output_dir: Path,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
        split: str = "full",
        source_dir: Path | None = None,
        revision: str | None = None,
        **kwargs,
    ):
        """
        Args:
            output_dir: Directory to write the generated tasks to.
            limit: Maximum number of tasks to generate.
            overwrite: Whether to overwrite existing task directories.
            task_ids: Only generate these tasks (Harbor ids such as ``mm-26`` or
                upstream ids such as ``MM-26``).
            split: ``full`` (all 2,000 MM test questions) or ``parity``
                (the stratified 10% parity subset).
            source_dir: Optional local copy of the dataset holding the question
                JSONL and either an ``images/`` directory or ``images.zip``.
                When omitted the data is downloaded from HuggingFace.
            revision: HuggingFace dataset revision (commit, tag, branch).
                Defaults to the pinned commit ``HF_REVISION``.
        """
        if split not in ("full", "parity"):
            raise ValueError(f"Unknown split: {split}")
        self.output_dir = Path(output_dir)
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = task_ids
        self.split = split
        self.source_dir = Path(source_dir) if source_dir else None
        self.revision = revision or HF_REVISION

    # ------------------------------------------------------------------ data

    def _locate_source(self) -> tuple[Path, ImageStore]:
        """Return the question file and an image store, downloading if needed."""
        if self.source_dir is not None:
            questions = next(
                (
                    self.source_dir / rel
                    for rel in _LOCAL_QUESTION_FILES
                    if (self.source_dir / rel).is_file()
                ),
                None,
            )
            if questions is None:
                raise FileNotFoundError(
                    f"No question file in {self.source_dir}; expected one of "
                    f"{', '.join(_LOCAL_QUESTION_FILES)}"
                )
            image_dir = self.source_dir / "images"
            image_zip = self.source_dir / "images.zip"
            store = ImageStore(
                directory=image_dir if image_dir.is_dir() else None,
                archive=image_zip if image_zip.is_file() else None,
            )
            return questions, store

        from huggingface_hub import hf_hub_download

        logger.info("Downloading %s from HuggingFace...", HF_REPO_ID)
        questions = hf_hub_download(
            HF_REPO_ID, HF_QUESTIONS_FILE, repo_type="dataset", revision=self.revision
        )
        images = hf_hub_download(
            HF_REPO_ID, HF_IMAGES_FILE, repo_type="dataset", revision=self.revision
        )
        return Path(questions), ImageStore(archive=Path(images))

    def _load_records(self, questions_path: Path) -> list[MedXpertQARecord]:
        records: list[MedXpertQARecord] = []
        with questions_path.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    records.append(normalize_record(json.loads(line)))
        records.sort(key=_source_order)

        task_ids = [r.task_id for r in records]
        if len(set(task_ids)) != len(task_ids):
            raise ValueError("Upstream ids do not map to unique task ids")
        return records

    def _select(self, records: list[MedXpertQARecord]) -> list[MedXpertQARecord]:
        if self.split == "parity":
            records = select_parity_subset(records)
        if self.task_ids:
            wanted = {make_task_id(t) for t in self.task_ids}
            unknown = wanted - {r.task_id for r in records}
            if unknown:
                logger.warning(
                    "Task ids not found in the %s split: %s",
                    self.split,
                    ", ".join(sorted(unknown)),
                )
            records = [r for r in records if r.task_id in wanted]
        if self.limit is not None:
            records = records[: self.limit]
        return records

    # ------------------------------------------------------------ generation

    @staticmethod
    def _render(text: str, values: dict[str, str]) -> str:
        """Single-pass ``{{KEY}}`` substitution (inserted values are never re-scanned)."""

        def replace(match: re.Match[str]) -> str:
            key = match.group(1)
            return values[key] if key in values else match.group(0)

        return re.sub(r"\{\{([A-Z_]+)\}\}", replace, text)

    @staticmethod
    def _toml_escape(value: str) -> str:
        """Escape a value for use inside a TOML basic (double-quoted) string."""
        return json.dumps(value, ensure_ascii=False)[1:-1]

    def _generate_task(self, record: MedXpertQARecord, images: ImageStore) -> None:
        task_dir = self.output_dir / record.task_id
        if task_dir.exists():
            shutil.rmtree(task_dir)
        shutil.copytree(
            TEMPLATE_DIR,
            task_dir,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )

        letters = list(record.options)

        # environment/images: the question's images, original file names
        image_dir = task_dir / "environment" / "images"
        image_dir.mkdir(parents=True, exist_ok=True)
        for image_name in record.images:
            images.write(image_name, image_dir / image_name)

        # instruction.md
        instruction_path = task_dir / "instruction.md"
        instruction_path.write_text(
            self._render(
                instruction_path.read_text(encoding="utf-8"),
                {
                    "QUESTION": record.question.strip(),
                    "IMAGE_LIST": "\n".join(
                        f"- `/app/images/{name}`" for name in record.images
                    ),
                    "START": letters[0],
                    "END": letters[-1],
                },
            ),
            encoding="utf-8",
        )

        # task.toml
        config_path = task_dir / "task.toml"
        config_path.write_text(
            self._render(
                config_path.read_text(encoding="utf-8"),
                {
                    "TASK_ID": record.task_id,
                    "SOURCE_ID": self._toml_escape(record.source_id),
                    "MEDICAL_TASK": self._toml_escape(record.medical_task),
                    "BODY_SYSTEM": self._toml_escape(record.body_system),
                    "QUESTION_TYPE": self._toml_escape(record.question_type),
                    "NUM_IMAGES": str(len(record.images)),
                },
            ),
            encoding="utf-8",
        )

        # solution/solve.sh
        solve_path = task_dir / "solution" / "solve.sh"
        solve_path.write_text(
            self._render(
                solve_path.read_text(encoding="utf-8"), {"ANSWER": record.label}
            ),
            encoding="utf-8",
        )
        solve_path.chmod(0o755)

        # tests/: ground truth in the shape the upstream scorer expects
        tests_dir = task_dir / "tests"
        (tests_dir / "test.sh").chmod(0o755)
        ground_truth = {
            "id": record.source_id,
            "options": [
                {"letter": letter, "content": content}
                for letter, content in record.options.items()
            ],
            "label": [record.label],
            "medical_task": record.medical_task,
            "body_system": record.body_system,
            "question_type": record.question_type,
        }
        (tests_dir / "ground_truth.json").write_text(
            json.dumps(ground_truth, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def run(self) -> list[Path]:
        """Generate Harbor tasks under ``self.output_dir``; returns their paths."""
        questions_path, images = self._locate_source()
        try:
            records = self._select(self._load_records(questions_path))
            self.output_dir.mkdir(parents=True, exist_ok=True)
            logger.info(
                "Generating %d MedXpertQA-MM tasks (%s split) in %s",
                len(records),
                self.split,
                self.output_dir,
            )

            generated: list[Path] = []
            skipped = 0
            for index, record in enumerate(records, 1):
                task_dir = self.output_dir / record.task_id
                if task_dir.exists() and not self.overwrite:
                    skipped += 1
                    continue
                self._generate_task(record, images)
                generated.append(task_dir)
                if index % 200 == 0:
                    logger.info("  %d/%d", index, len(records))
        finally:
            images.close()

        logger.info("Generated %d tasks", len(generated))
        if skipped:
            logger.info(
                "Skipped %d existing tasks (pass --overwrite to regenerate)", skipped
            )
        return generated
