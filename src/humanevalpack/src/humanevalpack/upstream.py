"""What the original HumanEvalPack harness does, ported without changes.

Sources (the pins the adapter was written and verified against):

* Dataset: https://huggingface.co/datasets/bigcode/humanevalpack at revision
  ``DATASET_REVISION`` (MIT license, six configs with 164 problems each).
* Task definitions: ``bigcode_eval/tasks/humanevalpack.py`` in
  https://github.com/bigcode-project/bigcode-evaluation-harness at commit
  ``HARNESS_COMMIT``. Line numbers in this module refer to that file.
* Scoring: the ``code_eval_octopack`` metric,
  https://huggingface.co/spaces/Muennighoff/code_eval_octopack at commit
  ``METRIC_COMMIT`` (its ``execute.py`` is vendored into every task's tests).

Only the default ``--prompt instruct`` format is ported; the other formats in
the harness are model-specific chat templates around the same content.
"""

from __future__ import annotations

from collections.abc import Mapping

DATASET_REPO = "bigcode/humanevalpack"
DATASET_REVISION = "9a41762f73a8cb23bb5811b73d5aab164efcf378"
HARNESS_COMMIT = "8fc5bae6479c4fbbb28c3f8b644f6a15b3f3b5bd"
METRIC_COMMIT = "fee8e8d8ff689a4489e153233778dd36f865e7aa"

# Dataset configs, in the order the paper and the harness documentation list them.
LANGUAGES = ("python", "js", "java", "go", "cpp", "rust")

# humanevalpack.py lines 18-25.
LANGUAGE_TO_NAME = {
    "python": "Python",
    "cpp": "C++",
    "js": "JavaScript",
    "java": "Java",
    "go": "Go",
    "rust": "Rust",
}

# humanevalpack.py lines 27-34.
LANGUAGE_TO_EXTENSION = {
    "python": "py",
    "cpp": "cpp",
    "js": "js",
    "java": "java",
    "go": "go",
    "rust": "rs",
}

# humanevalpack.py lines 51-58 (seconds; verify.py carries the same table).
LANGUAGE_TO_TIMEOUT = {
    "python": 10,
    "cpp": 60,
    "js": 10,
    "java": 10,
    "go": 20,
    "rust": 300,
}

# humanevalpack.py lines 90-101: the only Go imports a problem may declare.
GO_IMPORT_HELPER = (
    "math",
    "strings",
    "fmt",
    "strconv",
    "time",
    "bytes",
    "regexp",
    "sort",
    "math/rand",
    "crypto/md5",
)

Doc = Mapping[str, str]


def get_prompt_base(doc: Doc, language: str, with_docs: bool) -> str:
    """``HumanEvalPack.get_prompt_base`` (lines 189-197)."""
    if with_docs:
        return doc["prompt"]  # Already includes fn main for rust
    if language == "rust":
        return "fn main(){}\n" + doc["declaration"]
    return doc["declaration"]


def instruct_prompt(
    prompt_base: str, instruction: str, context: str | None = None
) -> str:
    """``HumanEvalPack.get_prompt`` for ``prompt == "instruct"`` (lines 199-263)."""
    if context is None:
        inp = instruction
    else:
        # `Context first then instruction` methods
        inp = context + "\n" + instruction
    prompt = inp + "\n\n" + prompt_base
    return prompt.strip()


def fix_prompt(doc: Doc, language: str, with_docs: bool) -> str:
    """``HumanEvalFixBase.get_prompt`` (lines 509-531).

    ``with_docs=False`` is ``humanevalfixtests-*`` (the unit tests are the source
    of truth); ``with_docs=True`` is ``humanevalfixdocs-*`` (the docstring is).
    """
    prompt_base = get_prompt_base(doc, language, with_docs)
    instruction = f"Fix bugs in {doc['entry_point']}."
    context = prompt_base + doc["buggy_solution"]
    if with_docs is False:  # Add tests as source of ground truth
        context += "\n" + doc["test"]
    return instruct_prompt(prompt_base, instruction, context).strip()


def synthesize_prompt(doc: Doc, language: str) -> str:
    """``HumanEvalSynthesizeBase.get_prompt`` (lines 674-679); ``with_docs`` is True."""
    prompt_base = get_prompt_base(doc, language, True)
    instruction = doc["instruction"].strip()
    return instruct_prompt(prompt_base, instruction)


def reference_solution(doc: Doc) -> str:
    """``HumanEvalPack.get_reference(doc, get_solution=True)`` (lines 265-267).

    This is the program the harness scores with ``--check_references``.
    """
    return doc["prompt"] + doc["canonical_solution"]


def go_imports_are_supported(doc: Doc) -> bool:
    """The check ``process_results`` asserts for every Go problem (lines 412-414)."""
    for line in doc["import"].split("\n"):
        line = (
            line.replace("import", "")
            .replace("(", "")
            .replace(")", "")
            .replace('"', "")
            .strip()
        )
        if line and line not in GO_IMPORT_HELPER:
            return False
    return True
