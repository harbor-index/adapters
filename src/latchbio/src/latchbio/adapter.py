"""Generate Harbor tasks from LatchBio's benchmarks.bio evaluation suite.

Each public LatchBio evaluation is a JSON file with a natural-language task, a
set of Latch Data nodes holding the input data, and a deterministic grader that
maps a structured JSON answer to pass/fail. The upstream harness
(latch-eval-tools) runs an agent in a container with the data mounted at
``/workspace/data`` and reads the answer from ``/workspace/eval_answer.json``.

The generated Harbor task reproduces that contract one to one:

- ``instruction.md``  the upstream task text followed by the harness's standard
  data instructions, i.e. the exact prompt upstream hands to CLI agents;
- ``environment/``    LatchBio's published agent image (pinned by digest) with the
  evaluation data staged at ``/workspace/data``;
- ``tests/``          a verifier that applies the upstream graders (the pinned
  latch-eval-tools release installed in the image) to
  ``/workspace/eval_answer.json`` with the eval's own grader config;
- ``solution/``       the reference answer implied by the grader's ground truth.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from .data import (
    DATA_SOURCES,
    DOCKERFILE_DATA_STAGES,
    SMALL_DATA_MAX_BYTES,
    SPLITS,
    DataError,
    DataManifest,
    DataNode,
    default_data_dir,
    download_node,
    resolve_nodes,
    stage_local_files,
    task_manifest,
)
from .graders_release import GradersReleaseError, data_instructions
from .oracle import load_grade_module, synthesize_oracle_answer
from .sources import (
    AGENT_IMAGE,
    BENCHMARKS,
    BENCHMARKS_BY_KEY,
    LATCH_EVAL_TOOLS_COMMIT,
    LATCH_EVAL_TOOLS_REPO,
    LATCH_EVAL_TOOLS_VERSION,
    LATCH_EVAL_TOOLS_WHEEL_SHA256,
    ORG,
    BenchmarkSource,
)
from .upstream import UpstreamEval, ensure_checkout, load_evals

TEMPLATE_DIR = Path(__file__).parent / "task-template"


def default_cache_dir() -> Path:
    return Path.home() / ".cache" / "harbor-latchbio" / "upstream"


def _toml(value: Any) -> str:
    """Render a scalar or a flat list as a TOML value."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        # JSON string escapes are a subset of TOML basic-string escapes.
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml(item) for item in value) + "]"
    raise TypeError(f"Cannot render {type(value).__name__} as TOML")


def _fill(template: str, values: dict[str, str]) -> str:
    """Substitute ``{name}`` placeholders in a single pass.

    One pass matters: upstream text is inserted verbatim and may itself contain
    brace sequences, which must not be treated as placeholders afterwards.
    """
    pattern = re.compile("|".join(re.escape("{" + name + "}") for name in values))
    return pattern.sub(lambda match: values[match.group(0)[1:-1]], template)


def _labels(value: Any) -> list[str]:
    """Upstream metadata fields such as ``kit`` are a string or a list of strings."""
    items = value if isinstance(value, (list, tuple)) else [value]
    return [str(item) for item in items if isinstance(item, str) and item and item != "none"]


def _unique(values: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for value in values:
        if value:
            seen.setdefault(value, None)
    return list(seen)


def _grader_types(specs: list[dict[str, Any]]) -> list[str]:
    """Top-level and nested grader types used by an eval, in first-seen order."""
    found: list[str] = []

    def visit(spec: dict[str, Any]) -> None:
        found.append(str(spec.get("type")))
        for child in (spec.get("config") or {}).get("children") or []:
            visit(child)

    for spec in specs:
        visit(spec)
    return _unique(found)


class LatchBioAdapter:
    """Convert the public LatchBio (benchmarks.bio) evaluations into Harbor tasks."""

    def __init__(
        self,
        output_dir: Path,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
        *,
        benchmarks: list[str] | None = None,
        split: str = "full",
        data_source: str = "mirror",
        data_dir: Path | None = None,
        upstream_root: Path | None = None,
        cache_dir: Path | None = None,
    ):
        """
        Args:
            output_dir: Directory to write the generated tasks to.
            limit: Generate at most this many tasks (after filtering).
            overwrite: Replace task directories that already exist.
            task_ids: Only generate these tasks. Accepts Harbor task ids
                (``scbench__de01_pseudobulk_de``), registry names
                (``latchbio/scbench__de01_pseudobulk_de``) or upstream eval ids
                (``DE01_pseudobulk_de``).
            benchmarks: Only generate tasks of these benchmarks (keys of
                ``sources.BENCHMARKS``).
            split: ``full`` for every adapted evaluation, or ``small-data`` for
                the evaluations whose input data totals at most
                ``SMALL_DATA_MAX_BYTES``.
            data_source: Where evaluation data comes from; see ``latchbio.data``.
            data_dir: Local cache for data downloaded with ``--data-source latch``.
            upstream_root: Directory holding existing clones of the upstream
                repositories at their pinned commits (skips fetching).
            cache_dir: Where upstream repositories are fetched to.
        """
        if data_source not in DATA_SOURCES:
            raise ValueError(f"data_source must be one of {DATA_SOURCES}, got {data_source!r}")
        if split not in SPLITS:
            raise ValueError(f"split must be one of {SPLITS}, got {split!r}")
        unknown = sorted(set(benchmarks or []) - set(BENCHMARKS_BY_KEY))
        if unknown:
            raise ValueError(
                f"Unknown benchmark(s) {unknown}; available: {sorted(BENCHMARKS_BY_KEY)}"
            )

        self.output_dir = Path(output_dir)
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = task_ids
        self.benchmarks = benchmarks
        self.split = split
        self.data_source = data_source
        self.data_dir = Path(data_dir) if data_dir else default_data_dir()
        self.upstream_root = Path(upstream_root) if upstream_root else None
        self.cache_dir = Path(cache_dir) if cache_dir else default_cache_dir()
        self.manifest = DataManifest.load()
        self.data_instructions = data_instructions()

    # ------------------------------------------------------------------ evals

    def sources(self) -> list[BenchmarkSource]:
        if not self.benchmarks:
            return list(BENCHMARKS)
        return [source for source in BENCHMARKS if source.key in self.benchmarks]

    def load_all_evals(self) -> list[UpstreamEval]:
        """Every adaptable eval of the selected benchmarks, in a stable order."""
        evals: list[UpstreamEval] = []
        for source in self.sources():
            checkout = ensure_checkout(
                source, cache_dir=self.cache_dir, upstream_root=self.upstream_root
            )
            evals.extend(load_evals(source, checkout))
        return evals

    def data_size(self, item: UpstreamEval) -> int:
        """Total size in bytes of an eval's input data, from the pinned manifest."""
        missing = [uri for uri in item.data_nodes if uri not in self.manifest.nodes]
        if missing:
            raise DataError(
                f"{item.eval_id}: data node(s) {missing} are not in data_manifest.json, "
                "so the size of its input data is unknown."
            )
        nodes = resolve_nodes([self.manifest.nodes[uri] for uri in item.data_nodes])
        return sum(node.size for node in nodes)

    def in_small_data_split(self, item: UpstreamEval) -> bool:
        return self.data_size(item) <= SMALL_DATA_MAX_BYTES

    def select_evals(self) -> list[UpstreamEval]:
        evals = self.load_all_evals()
        if self.split == "small-data":
            evals = [item for item in evals if self.in_small_data_split(item)]
        if self.task_ids:
            wanted = {str(task_id) for task_id in self.task_ids}
            selected = [
                item
                for item in evals
                if wanted & {item.task_id, item.task_name, item.eval_id}
            ]
            matched = {
                key
                for item in selected
                for key in (item.task_id, item.task_name, item.eval_id)
            }
            missing = sorted(wanted - matched)
            if missing:
                raise ValueError(f"No such task(s): {missing}")
            evals = selected
        if self.limit is not None:
            evals = evals[: max(self.limit, 0)]
        return evals

    # ------------------------------------------------------------------- data

    def _nodes_for(self, item: UpstreamEval) -> list[DataNode]:
        """Pinned descriptions of an eval's data nodes, per the data source."""
        nodes: list[DataNode] = []
        for uri in item.data_nodes:
            pinned = self.manifest.nodes.get(uri)
            if self.data_source == "latch":
                node = download_node(uri, self.data_dir)
                if pinned is not None and pinned.to_json() != node.to_json():
                    raise DataError(
                        f"{uri} downloaded from Latch no longer matches "
                        "data_manifest.json (name, file list or checksums changed "
                        "upstream). Rebuild the manifest with "
                        "scripts/build_data_manifest.py and review the diff."
                    )
                nodes.append(node)
            else:
                if pinned is None:
                    raise DataError(
                        f"{uri} (eval {item.eval_id}) is not in data_manifest.json. "
                        "Run scripts/build_data_manifest.py with Latch credentials, "
                        "or generate with --data-source latch."
                    )
                nodes.append(pinned)
        return resolve_nodes(nodes)

    # ------------------------------------------------------------------ tasks

    def run(self) -> None:
        """Generate one Harbor task directory per selected evaluation."""
        evals = self.select_evals()
        if self.data_source == "mirror" and not self.manifest.mirror_base_url:
            raise DataError(
                "data_manifest.json does not name a data mirror yet, so "
                "--data-source mirror cannot stage evaluation data. Use "
                "--data-source latch (requires `latch login`), or "
                "--data-source none to exercise only the oracle and verifier."
            )

        self.output_dir.mkdir(parents=True, exist_ok=True)
        generated = skipped = 0
        for item in evals:
            task_dir = self.output_dir / item.task_id
            if task_dir.exists() and not self.overwrite:
                print(f"skip      {item.task_id} (exists; use --overwrite)")
                skipped += 1
                continue
            # Build the task next to its destination and move it into place only
            # once it is complete, so a failed generation never leaves a partial
            # task directory that a later run would skip as existing.
            scratch = Path(tempfile.mkdtemp(prefix=f".{item.task_id}.", dir=self.output_dir))
            try:
                self.generate_task(item, scratch / item.task_id)
                if task_dir.exists():
                    shutil.rmtree(task_dir)
                (scratch / item.task_id).rename(task_dir)
            finally:
                shutil.rmtree(scratch, ignore_errors=True)
            print(f"generated {item.task_id}")
            generated += 1

        print(
            f"\n{generated} task(s) generated, {skipped} skipped, "
            f"data source: {self.data_source} -> {self.output_dir}"
        )

    def eval_config(self, item: UpstreamEval) -> dict[str, Any]:
        """The grading contract shipped to the verifier as ``tests/eval_config.json``."""
        config: dict[str, Any] = {
            "id": item.eval_id,
            "benchmark": item.source.key,
            "upstream": {
                "repo": item.source.repo,
                "commit": item.source.commit,
                "path": item.path,
            },
        }
        if "canary" in item.raw:
            config["canary"] = item.raw["canary"]
        if item.raw.get("grader") is not None:
            config["grader"] = item.raw["grader"]
        else:
            config["graders"] = item.raw["graders"]
        return config

    def render_instruction(self, item: UpstreamEval) -> str:
        """Upstream's prompt for CLI agents: ``f"{task}\\n{data_instructions}"``.

        See ``latch_eval_tools.harness.utils.prompt_with_suffix`` and the
        ``prompt_suffix=load_data_instructions()`` default of the CLI harnesses.
        """
        template = (TEMPLATE_DIR / "instruction.md").read_text(encoding="utf-8")
        return _fill(
            template,
            {"task": item.raw["task"], "data_instructions": self.data_instructions},
        )

    def render_task_toml(self, item: UpstreamEval) -> str:
        source = item.source
        meta = item.metadata
        authors = "\n".join(
            "    { "
            + ", ".join(f"{key} = {_toml(value)}" for key, value in author.items())
            + " },"
            for author in source.authors
        )
        categories = _labels(meta.get("task"))
        kits = _labels(meta.get("kit"))
        details = ", ".join(["/".join(part) for part in (categories, kits) if part])
        description = f"{source.display_name} evaluation {item.eval_id}" + (
            f" ({details})" if details else ""
        )
        keywords = _unique(
            ["latchbio", "benchmarks-bio", source.key, "biology", "bioinformatics"]
            + categories
            + kits
        )
        metadata: dict[str, Any] = {
            "benchmark": source.key,
            "benchmark_name": source.display_name,
            "eval_id": item.eval_id,
        }
        for key, upstream_key in (
            ("eval_task", "task"),
            ("kit", "kit"),
            ("time_horizon", "time_horizon"),
            ("eval_type", "eval_type"),
        ):
            value = meta.get(upstream_key)
            if isinstance(value, str):
                metadata[key] = value
            elif isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
                metadata[key] = list(value)
        metadata.update(
            {
                "grader_types": _grader_types(item.graders),
                "data_nodes": item.data_nodes,
                "upstream_repo": source.repo,
                "upstream_commit": source.commit,
                "upstream_path": item.path,
                "harness": f"latch-eval-tools=={LATCH_EVAL_TOOLS_VERSION}",
                "harness_repo": LATCH_EVAL_TOOLS_REPO,
                "harness_commit": LATCH_EVAL_TOOLS_COMMIT,
            }
        )

        rendered = _fill(
            (TEMPLATE_DIR / "task.toml").read_text(encoding="utf-8"),
            {
                "org": ORG,
                "task_id": item.task_id,
                "description": _toml(description),
                "authors": authors,
                "keywords": _toml(keywords),
                "metadata": "\n".join(f"{k} = {_toml(v)}" for k, v in metadata.items()),
            },
        )

        parsed = tomllib.loads(rendered)
        if parsed["task"]["name"] != item.task_name:
            raise ValueError(f"Rendered task.toml has the wrong name for {item.task_id}")
        return rendered

    def generate_task(self, item: UpstreamEval, task_dir: Path) -> None:
        eval_config = self.eval_config(item)

        # Reject unsupported or malformed graders before writing anything, and
        # never generate tasks with graders other than the pinned release.
        try:
            grade = load_grade_module()
        except RuntimeError as exc:
            raise GradersReleaseError(str(exc)) from exc
        for spec in item.graders:
            if spec.get("type") not in grade.GRADER_REGISTRY:
                raise ValueError(
                    f"{item.task_id}: grader type {spec.get('type')!r} is not a "
                    "deterministic latch-eval-tools grader"
                )
        oracle_answer = synthesize_oracle_answer(eval_config)
        nodes = [] if self.data_source == "none" else self._nodes_for(item)

        environment_dir = task_dir / "environment"
        solution_dir = task_dir / "solution"
        tests_dir = task_dir / "tests"
        for directory in (environment_dir, solution_dir, tests_dir):
            directory.mkdir(parents=True)

        (task_dir / "instruction.md").write_text(self.render_instruction(item), encoding="utf-8")
        (task_dir / "task.toml").write_text(self.render_task_toml(item), encoding="utf-8")

        # environment/
        dockerfile = _fill(
            (TEMPLATE_DIR / "environment" / "Dockerfile").read_text(encoding="utf-8"),
            {
                "agent_image": AGENT_IMAGE,
                "data_stage": DOCKERFILE_DATA_STAGES[self.data_source],
                "latch_eval_tools_version": LATCH_EVAL_TOOLS_VERSION,
                "latch_eval_tools_wheel_sha256": LATCH_EVAL_TOOLS_WHEEL_SHA256,
            },
        )
        (environment_dir / "Dockerfile").write_text(dockerfile, encoding="utf-8")
        if self.data_source != "none":
            shutil.copy2(
                TEMPLATE_DIR / "environment" / "fetch_data.py",
                environment_dir / "fetch_data.py",
            )
            manifest = task_manifest(
                nodes, self.manifest, with_urls=self.data_source == "mirror"
            )
            (environment_dir / "data_manifest.json").write_text(
                json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
            )
            if self.data_source == "latch":
                stage_local_files(nodes, self.data_dir, environment_dir / "data")

        # solution/
        solve = solution_dir / "solve.sh"
        shutil.copy2(TEMPLATE_DIR / "solution" / "solve.sh", solve)
        solve.chmod(0o755)
        (solution_dir / "eval_answer.json").write_text(
            json.dumps(oracle_answer, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        notes = item.raw.get("notes")
        if isinstance(notes, str) and notes.strip():
            (solution_dir / "upstream_notes.md").write_text(
                f"# Upstream notes for `{item.eval_id}`\n\n"
                f"Verbatim `notes` field of `{item.path}` in {item.source.repo} at "
                f"`{item.source.commit}`. It documents how LatchBio derived the "
                "ground truth that `eval_answer.json` reproduces.\n\n"
                f"{notes.rstrip()}\n",
                encoding="utf-8",
            )

        # tests/
        for name in ("test.sh", "grade.py"):
            shutil.copy2(TEMPLATE_DIR / "tests" / name, tests_dir / name)
            (tests_dir / name).chmod(0o755)
        shutil.copy2(TEMPLATE_DIR / "tests" / "graders.lock.json", tests_dir / "graders.lock.json")
        (tests_dir / "eval_config.json").write_text(
            json.dumps(eval_config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
