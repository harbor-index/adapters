"""Pinned upstream sources for the LatchBio (benchmarks.bio) adapter.

LatchBio publishes each benchmark of the benchmarks.bio suite in its own GitHub
repository. Most evaluations are withheld to prevent training contamination;
each repository releases a small public subset. This module pins, per
benchmark, the exact upstream commit the adapter reads from, so regenerated
datasets are byte-stable.

Only benchmarks whose public release contains *both* the input data pointers
(``data_node``) and a deterministic grader (``grader`` / ``graders``) can be
adapted. The adapter README lists every other benchmark of the suite together
with the reason it is left out.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Harness + grader implementation the adapter mirrors (graders, prompt suffix,
# answer-file protocol). The adapter and every task image install this PyPI
# release; see latchbio.graders_release.
LATCH_EVAL_TOOLS_REPO = "https://github.com/latchbio/latch-eval-tools"
LATCH_EVAL_TOOLS_COMMIT = "0d7983126cbc6e298b9865de32b1f0d988aee1a5"
LATCH_EVAL_TOOLS_VERSION = "0.4.54"
# SHA-256 of latch_eval_tools-0.4.54-py3-none-any.whl on PyPI (also in uv.lock).
LATCH_EVAL_TOOLS_WHEEL_SHA256 = "41ac5b8c75b73a990b8672c07d528b2e9a2efe8e929e4c0de40a09340008587a"

# Agent container published by LatchBio (latch-eval-tools/agent_env/Dockerfile).
# Upstream runs every evaluation in ``benchmark_agent:latest``; the adapter pins
# the multi-arch index digest that tag resolved to when this adapter was built.
# The image is linux/amd64 only.
AGENT_IMAGE_REPOSITORY = "public.ecr.aws/p5z7v3z8/benchmark_agent"
AGENT_IMAGE_DIGEST = (
    "sha256:552728033c6c32d99094e848d8fb831cddc52c17ab6b10badaebe0aa301baf7c"
)
AGENT_IMAGE = f"{AGENT_IMAGE_REPOSITORY}@{AGENT_IMAGE_DIGEST}"

# Registry organisation and attribution fallback.
ORG = "latchbio"
LATCHBIO_AUTHOR = {"name": "LatchBio"}


@dataclass(frozen=True)
class BenchmarkSource:
    """One upstream benchmark repository pinned to a commit."""

    key: str
    """Short, registry-safe benchmark identifier (also the task-name prefix)."""
    display_name: str
    repo: str
    commit: str
    evals_dir: str
    """Directory in the repository that holds the public eval JSON files."""
    authors: tuple[dict[str, str], ...]
    """Benchmark authors, from the upstream README citation where one exists."""
    non_eval_files: tuple[str, ...] = ("manifest.json", "vocabulary.json")
    """File names upstream keeps next to the evals that are not eval definitions."""
    excluded_eval_ids: dict[str, str] = field(default_factory=dict)
    """Public evals deliberately skipped, mapped to the reason."""


# VariantBench's two neoantigen evals: upstream states they are released publicly
# but are not part of the main 118-eval benchmark, and every Latch Data node they
# reference returns "no such Latch file or directory" (checked 2026-11-22 with a
# standard Latch account), so their inputs cannot be staged.
_DATA_UNREADABLE = (
    "Every data_node of this eval is unreadable on Latch Data "
    "(LatchPathError: no such Latch file or directory), so the inputs cannot be "
    "staged. Upstream also notes it is not part of the main 118-eval benchmark."
)


def _authors(*names: str) -> tuple[dict[str, str], ...]:
    return tuple({"name": name} for name in names)


BENCHMARKS: tuple[BenchmarkSource, ...] = (
    BenchmarkSource(
        key="scbench",
        display_name="scBench",
        repo="https://github.com/latchbio/scbench",
        commit="106726f432e0221a3fdbf9ef8e39b21f638f9b60",
        evals_dir="evals",
        authors=_authors(
            "Kenny Workman",
            "Zhen Yang",
            "Harihara Muralidharan",
            "Aidan Abdulali",
            "Hannah Le",
        ),
    ),
    BenchmarkSource(
        key="spatialbench",
        display_name="SpatialBench",
        repo="https://github.com/latchbio/spatialbench",
        commit="baa9841efc99fa03b2f9ec25684c8ec52a2245df",
        evals_dir="example_evals",
        authors=_authors(
            "Kenny Workman",
            "Zhen Yang",
            "Harihara Muralidharan",
            "Hannah Le",
        ),
    ),
    BenchmarkSource(
        key="variantbench",
        display_name="VariantBench",
        repo="https://github.com/latchbio/variantbench",
        commit="96a26565d7ff08961a2bbdd1061cd5957a9b3692",
        evals_dir="evals",
        authors=_authors(
            "Aashka Bhowmick",
            "Nasim Rahmatpour",
            "Shivang Sharma",
            "Qian Xu",
            "Ananya Gupta",
            "Asmita Lagwankar",
            "Arjun Banerjee",
            "Christopher Zou",
        ),
        excluded_eval_ids={
            "eval_stage4_annotated_vcf": _DATA_UNREADABLE,
            "osteosarcoma_wes_stage3_called_vcf_neoantigen_gene_recall": _DATA_UNREADABLE,
        },
    ),
    BenchmarkSource(
        key="metagenomicsbench",
        display_name="MetagenomicsBench",
        repo="https://github.com/latchbio/metagenomicsbench",
        commit="661be73c5255f11b4d5973afbbfb6f4501646b5d",
        evals_dir="evals",
        authors=(LATCHBIO_AUTHOR,),
    ),
    BenchmarkSource(
        key="txbench-pp",
        display_name="TxBench-Preclinical-Pharmacology",
        repo="https://github.com/latchbio/txbench-pp",
        commit="920bff72157cb2a6106ff4f534f160eccca8ad85",
        evals_dir="evals",
        authors=(LATCHBIO_AUTHOR,),
    ),
)

BENCHMARKS_BY_KEY: dict[str, BenchmarkSource] = {b.key: b for b in BENCHMARKS}
