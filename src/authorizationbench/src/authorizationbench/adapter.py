"""Generate Harbor tasks from AuthorizationBench task bundles.

Each upstream bundle (``authorization_eva_sandbox/tasks/<task_id>/``) becomes one
Harbor task:

* ``environment/authz-env/`` - sidecar running the upstream mock services, the
  Oracle User and an MCP gateway with the task's allowlisted tools. It holds the
  task contract (``task_config.json``) and seeds the shared ``/workspace``.
* ``environment/Dockerfile`` - the agent container (workspace volume, rootfs
  files and tool wrappers the upstream runner stages).
* ``instruction.md`` - the upstream "native" prompt addition (sealed prompt +
  ENVIRONMENT_NOTES) followed by the user's instruction.
* ``tests/`` - separate verifier running the upstream completion and
  out-of-authorization judges.
* ``solution/`` - replay of the upstream scripted authorized path.

Upstream code is used at a pinned commit so prompts, tool allowlists and
grading match the original benchmark.
"""

from __future__ import annotations

import csv
import importlib
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

AUTHZ_REPO = "https://github.com/PEACH-Research-Lab/AuthorizationBench.git"
AUTHZ_COMMIT = "bfe1fc27b513d47dd02a0b5625ccb4f9a2b0e47c"
DATASET_ORG = "peach-research-lab"
DATASET_NAME = "authorizationbench"

SANDBOX = "authorization_eva_sandbox"
RECEIPTS = f"{SANDBOX}/task_generation/stages/accept/scripted-path-receipts-and-eval"
TEMPLATE_DIR = Path(__file__).resolve().parent / "task-template"
DEFAULT_CACHE = Path.home() / ".cache" / "harbor-authorizationbench"


def _toml_str(value: object) -> str:
    """A TOML basic string (JSON string escapes are valid TOML escapes)."""
    return json.dumps(str(value), ensure_ascii=False)


def ensure_source(source_dir: Path | None, cache_dir: Path = DEFAULT_CACHE) -> Path:
    """Return an AuthorizationBench checkout at AUTHZ_COMMIT, cloning if needed."""
    if source_dir is not None:
        source = Path(source_dir).expanduser().resolve()
        if not (source / "benchmark" / "manifest.json").is_file():
            raise FileNotFoundError(f"{source} is not an AuthorizationBench checkout")
        return source
    source = cache_dir / AUTHZ_COMMIT
    if (source / "benchmark" / "manifest.json").is_file():
        return source
    source.mkdir(parents=True, exist_ok=True)
    for args in (
        ["init", "-q"],
        ["remote", "add", "origin", AUTHZ_REPO],
        ["fetch", "-q", "--depth", "1", "origin", AUTHZ_COMMIT],
        ["checkout", "-q", "FETCH_HEAD"],
    ):
        subprocess.run(["git", "-C", str(source), *args], check=True)
    return source


class _Upstream:
    """The upstream helpers that define tool allowlists and prompt text."""

    def __init__(self, source: Path):
        for path in (str(source), str(source / SANDBOX)):
            if path not in sys.path:
                sys.path.insert(0, path)
        harness = f"{SANDBOX}.evaluation.harness"
        tool_ir = importlib.import_module(f"{harness}.tool_ir")
        names = importlib.import_module(f"{harness}.tool_name_adapter")
        prompts = importlib.import_module(f"{harness}.prompt_composition")
        self.allowlist_from_actions = tool_ir.allowlist_from_actions
        self.flatten_actions = tool_ir._flatten_actions
        self.ToolNameAdapter = names.ToolNameAdapter
        self.native_addition = prompts.native_addition
        self.ENVIRONMENT_NOTES = prompts.ENVIRONMENT_NOTES


@dataclass(frozen=True)
class _Lineage:
    source_case_id: str
    case_type: str


class AuthorizationBenchAdapter:
    def __init__(
        self,
        output_dir: Path,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
        source_dir: Path | None = None,
        **kwargs,
    ):
        self.output_dir = Path(output_dir)
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = task_ids
        self.source_dir = source_dir

    # -- selection ----------------------------------------------------------

    def _selected_ids(self, source: Path) -> list[str]:
        manifest = json.loads((source / "benchmark" / "manifest.json").read_text())
        ids = list(manifest["task_ids"])
        if self.task_ids:
            wanted = {t.lower().rsplit("__", 1)[-1] for t in self.task_ids}
            ids = [t for t in ids if t.lower() in wanted]
            missing = wanted - {t.lower() for t in ids}
            if missing:
                raise ValueError(f"unknown task ids: {sorted(missing)}")
        if self.limit is not None:
            ids = ids[: self.limit]
        return ids

    @staticmethod
    def _lineage(source: Path) -> dict[str, _Lineage]:
        with open(source / "benchmark" / "lineage.csv", newline="") as f:
            return {
                row["task_id"]: _Lineage(row["source_case_id"], row["case_type"])
                for row in csv.DictReader(f)
            }

    # -- generation ---------------------------------------------------------

    def run(self) -> None:
        source = ensure_source(self.source_dir)
        upstream = _Upstream(source)
        lineage = self._lineage(source)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        ids = self._selected_ids(source)
        written = 0
        for task_id in ids:
            if self._generate(source, upstream, lineage, task_id):
                written += 1
        print(f"Wrote {written} of {len(ids)} tasks to {self.output_dir}")

    def _generate(self, source: Path, upstream: _Upstream,
                  lineage: dict[str, _Lineage], task_id: str) -> bool:
        bundle = source / SANDBOX / "tasks" / task_id
        task_name = task_id.lower()
        out = self.output_dir / task_name
        if out.exists():
            if not self.overwrite:
                return False
            shutil.rmtree(out)
        shutil.copytree(TEMPLATE_DIR, out)

        config = json.loads((bundle / "task_config.json").read_text())
        vignette = json.loads((bundle / "vignette.json").read_text())
        actions = [step["action"] for step in vignette["assembly_blueprint"]["actions"]]
        allowlist, _ = upstream.allowlist_from_actions(actions)
        methods = config.get("detection_policy", {}).get("methods") or []
        wrappers = sorted(p.name for p in (bundle / "wrappers").glob("*")) \
            if (bundle / "wrappers").is_dir() else []

        self._write_instruction(out, bundle, config, allowlist, upstream)
        self._write_sidecar(out, bundle, allowlist, wrappers)
        self._write_agent_image(out, bundle, config, actions, wrappers, upstream)
        self._write_tests(out, bundle, allowlist, methods)
        self._write_solution(out, source, task_id, config)
        self._write_task_toml(out, task_id, config, vignette, lineage.get(task_id), wrappers)
        return True

    @staticmethod
    def _write_instruction(out: Path, bundle: Path, config: dict,
                           allowlist: list[str], upstream: _Upstream) -> None:
        # Upstream "native" condition: the harness keeps its own system prompt
        # and appends the sealed prompt (tool names adapted) + ENVIRONMENT_NOTES;
        # the instruction is the first user message. Harbor agents take one
        # instruction, so the addition comes first, then the instruction verbatim.
        sealed = (bundle / "system_prompt.txt").read_text(encoding="utf-8")
        sealed = upstream.ToolNameAdapter(allowlist).adapt_prompt(sealed)
        text = upstream.native_addition(sealed) + "\n" + str(config["instruction"]).strip() + "\n"
        (out / "instruction.md").write_text(text, encoding="utf-8")

    @staticmethod
    def _write_sidecar(out: Path, bundle: Path, allowlist: list[str],
                       wrappers: list[str]) -> None:
        sidecar = out / "environment" / "authz-env"
        target = sidecar / "bundle"
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(bundle / "task_config.json", target / "task_config.json")
        if (bundle / "fixtures").is_dir():
            shutil.copytree(bundle / "fixtures", target / "fixtures")
        (target / "mcp_allowlist.json").write_text(json.dumps(allowlist, indent=2) + "\n")

        # Workspace seed: the sealed workspace plus tool wrappers in bin/,
        # where the upstream runner stages them.
        workspace = sidecar / "workspace"
        if (bundle / "workspace").is_dir():
            shutil.copytree(bundle / "workspace", workspace)
        if wrappers:
            shutil.copytree(bundle / "wrappers", workspace / "bin", dirs_exist_ok=True)
        lines = ["RUN mkdir -p /workspace"]
        if workspace.is_dir() and any(workspace.iterdir()):
            lines.append("COPY workspace/ /workspace/")
        if wrappers:
            lines.append("RUN chmod +x /workspace/bin/*")
        dockerfile = sidecar / "Dockerfile"
        text = dockerfile.read_text().replace("__AUTHZ_COMMIT__", AUTHZ_COMMIT)
        text = text.replace(
            "# Seeds the shared `workspace` volume on first mount.\nCOPY workspace/ /workspace/",
            "# Seeds the shared `workspace` volume on first mount.\n" + "\n".join(lines),
        )
        dockerfile.write_text(text)

    @staticmethod
    def _write_agent_image(out: Path, bundle: Path, config: dict, actions: list,
                           wrappers: list[str], upstream: _Upstream) -> None:
        env_dir = out / "environment"
        # Run settings for the parity agent (authorizationbench_openhands.py).
        # Nothing here is part of the authorization contract: the shell tools
        # are named in the prompt, and the step cap and condenser are harness
        # settings.
        agent_task = {
            "task_id": config["task_id"],
            "home": config.get("home") or "/workspace",
            "agent": config.get("agent") or {},
            "shell_actions": [a for a in upstream.flatten_actions(actions)
                              if a.startswith("runtime:shell.")],
        }
        (env_dir / "agent-task.json").write_text(json.dumps(agent_task, indent=2) + "\n")
        layers = ["COPY agent-task.json /opt/authz-agent/task.json"]
        if (bundle / "rootfs").is_dir():
            shutil.copytree(bundle / "rootfs", env_dir / "rootfs")
            layers.append("# Files the task places outside the workspace (upstream rootfs/).")
            layers.append("COPY rootfs/ /")
        if wrappers:
            layers.append("# Tool wrappers live in /workspace/bin (shared volume), as upstream stages them.")
            layers.append("RUN " + " && ".join(
                f"ln -sf /workspace/bin/{name} /usr/local/bin/{name}" for name in wrappers))
        dockerfile = env_dir / "Dockerfile"
        dockerfile.write_text(dockerfile.read_text().replace("__EXTRA_LAYERS__", "\n".join(layers)))

    @staticmethod
    def _write_tests(out: Path, bundle: Path, allowlist: list[str], methods: list[str]) -> None:
        tests = out / "tests"
        dockerfile = tests / "Dockerfile"
        dockerfile.write_text(dockerfile.read_text().replace("__AUTHZ_COMMIT__", AUTHZ_COMMIT))
        target = tests / "bundle"
        target.mkdir(parents=True, exist_ok=True)
        for name in ("task_config.json", "vignette.json"):
            shutil.copy2(bundle / name, target / name)
        # The detectors accept a bundle dir that carries resolved_task_config.json;
        # fixtures/ there feed the completion judge's seeded-state evidence.
        shutil.copy2(bundle / "task_config.json", target / "resolved_task_config.json")
        if (bundle / "fixtures").is_dir():
            shutil.copytree(bundle / "fixtures", target / "fixtures")
        (target / "mcp_allowlist.json").write_text(json.dumps(allowlist, indent=2) + "\n")
        # hash_diff compares the sealed workspace with the final one.
        if "hash_diff" in methods and (bundle / "workspace").is_dir():
            shutil.copytree(bundle / "workspace", target / "workspace")

    @staticmethod
    def _write_solution(out: Path, source: Path, task_id: str, config: dict) -> None:
        receipt_path = source / RECEIPTS / task_id / "authorized" / "receipt.json"
        receipt = json.loads(receipt_path.read_text())
        # Receipts name service tools by template; the environment and the MCP
        # allowlist use the service id. They differ for a few expanded tasks
        # (e.g. id shopping_app_venuepass, template shopping_app_instacart).
        by_template = {s["template"]: s["id"] for s in config.get("services") or []
                       if s.get("id") and s.get("template") and s["id"] != s["template"]}
        steps = []
        for step in receipt["steps"]:
            if step.get("execution") == "tool":
                tool = step["tool"]
                prefix, dot, operation = tool.partition(".")
                if dot and prefix in by_template:
                    tool = f"{by_template[prefix]}.{operation}"
                steps.append({"index": step["index"], "tool": tool,
                              "arguments": step.get("arguments") or {}})
            elif step.get("execution") == "user_message":
                steps.append({"index": step["index"], "tool": "user_message",
                              "content": str(step.get("result") or "")})
        path = {"task_id": task_id, "instruction": str(config["instruction"]),
                "upstream_receipt": f"{RECEIPTS}/{task_id}/authorized/receipt.json",
                "steps": steps}
        (out / "solution" / "authorized_path.json").write_text(
            json.dumps(path, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    @staticmethod
    def _write_task_toml(out: Path, task_id: str, config: dict, vignette: dict,
                         lineage: _Lineage | None, wrappers: list[str]) -> None:
        artifacts = [
            '{ source = "/collected", service = "authz-env", destination = "env" }',
            '{ source = "/workspace", destination = "workspace" }',
            '{ source = "/logs/agent", destination = "agent" }',
        ]
        if wrappers:
            artifacts.append(
                '{ source = "/tmp/tool_invocations.jsonl", destination = "tool_invocations.jsonl" }')
        domain = str(vignette.get("domain") or "")
        harm = str(config.get("harm") or "")
        values = {
            "task_name": f"{DATASET_ORG}/{DATASET_NAME}__{task_id.lower()}",
            "description": f"AuthorizationBench {task_id} ({domain}, direct harm: {harm})",
            "task_id": task_id,
            "source_case_id": lineage.source_case_id if lineage else "",
            "case_type": lineage.case_type if lineage else "",
            "domain": domain,
            "harm": harm,
            "trigger": str(config.get("situational_trigger") or ""),
        }
        path = out / "task.toml"
        text = path.read_text()
        for key, value in values.items():
            text = text.replace(f'"{{{key}}}"', _toml_str(value))
        text = text.replace(
            "artifacts = []",
            "artifacts = [\n" + "".join(f"    {a},\n" for a in artifacts) + "]",
        )
        path.write_text(text)
