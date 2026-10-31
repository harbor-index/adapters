"""Generate Harbor tasks from an AgentPersonaBench checkout."""

from __future__ import annotations

import logging
import re
import shutil
import tomllib
from pathlib import Path

from .oracle import OracleError, chat_solve_script, solve_script

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_TASK_TOML_READ = re.compile(r'(?:_TASK|TASK|TASK_DIR) / "task\.toml"')
_OTHER_TASK_TOML_READ = re.compile(r"""(?:/|join\()\s*["']task\.toml["']""")
_CHECKS_PATH = 'Path(__file__).resolve().parent / "checks.toml"'
_DOCKERFILE = """\
FROM python:3.12-bookworm

RUN pip install --no-cache-dir pyyaml==6.0.2

WORKDIR /app
COPY input/ /app/input/
RUN mkdir -p /app/output
"""
_REWARD_TAIL = """\
export ADHERENCE_OUTPUT_DIR=/app/output
export ADHERENCE_VERIFIER_DIR=/logs/verifier

# Drop a reward the agent may have pre-written. Keep a reward the verifier
# itself wrote: many verifiers record a real score and then exit 1.
rm -f /logs/verifier/reward.txt
set +e
python3 /tests/verifier.py
set -e

if [ ! -f /logs/verifier/reward.txt ]; then
  echo 0 > /logs/verifier/reward.txt
fi
exit 0
"""
_TEST_SH = """\
#!/bin/bash
set -euo pipefail

# Harbor copies tests to /tests. The upstream verifier finds the task root via
# Path(__file__).parents[1], so the questionnaire must also exist at /input.
# /tests/input is the pristine copy. /app/input is the agent-visible copy.
mkdir -p /logs/verifier /input
if [ -d /tests/input ]; then
  cp -a /tests/input/. /input/
elif [ -d /app/input ]; then
  cp -a /app/input/. /input/
fi

""" + _REWARD_TAIL
_INTERACTIVE_TEST_SH = """\
#!/bin/bash
set -euo pipefail

# Harbor copies tests to /tests, so Path(__file__).parents[1] is /. The verifier
# still reads the task tree from that root. Pristine input and environment
# copies live under /tests. The image copies are what the agent could edit.
mkdir -p /logs/verifier /input /environment
if [ -d /tests/input ]; then
  cp -a /tests/input/. /input/
elif [ -d /app/input ]; then
  cp -a /app/input/. /input/
fi
if [ -d /tests/environment ]; then
  cp -a /tests/environment/. /environment/
elif [ -d /opt/task-environment ]; then
  cp -a /opt/task-environment/. /environment/
fi
if [ -f /tests/instruction.md ]; then
  cp /tests/instruction.md /instruction.md
fi
if [ -f /tests/persona.yaml ]; then
  cp /tests/persona.yaml /persona.yaml
fi

""" + _REWARD_TAIL
_CHAT_TEST_SH = """\
#!/bin/bash
set -euo pipefail

# The copied verifier imports llm_client. That module is tests/llm_client.py.
# persona.yaml is copied here only after the agent has finished.
mkdir -p /logs/verifier /app/output /input
if [ -d /tests/input ]; then
  cp -a /tests/input/. /input/
elif [ -d /app/input ]; then
  cp -a /app/input/. /input/
fi
if [ -f /tests/instruction.md ]; then
  cp /tests/instruction.md /instruction.md
fi
if [ -f /tests/persona.yaml ]; then
  cp /tests/persona.yaml /persona.yaml
fi

export PYTHONPATH="/tests${PYTHONPATH:+:$PYTHONPATH}"
""" + _REWARD_TAIL
_CHAT_DOCKERFILE = """\
FROM python:3.12-bookworm

RUN pip install --no-cache-dir pyyaml==6.0.2

WORKDIR /app
COPY input/ /app/input/
RUN mkdir -p /app/output
"""
_WEB_BASE = (
    "evaluation/src/environment/task-environments/application/shared-web-playwright/Dockerfile"
)
_APP_BASE = (
    "evaluation/src/environment/task-environments/application/shared-os-app-linux/Dockerfile"
)


class AgentPersonaBenchAdapter:
    def __init__(
        self,
        output_dir: Path,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
        source: Path | None = None,
        surface: str = "survey",
        **kwargs,
    ):
        self.output_dir = Path(output_dir)
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = set(task_ids) if task_ids is not None else None
        self.source = Path(source) if source else None
        self.surface = surface
        self.generated_count = 0
        self.excluded: list[str] = []

    def run(self) -> None:
        if self.surface not in {"survey", "web", "app", "chat"}:
            raise SystemExit(f"surface {self.surface!r} is not survey, web, app, or chat")
        if self.source is None or not (self.source / "tasks").is_dir():
            raise SystemExit(
                "Pass --source pointing at an AgentPersonaBench checkout "
                "(the directory that contains tasks/)."
            )

        authors = _citation_authors(self.source)
        selected = []
        for task_dir in _tasks_for(self.source, self.surface):
            slug = task_dir.name
            rel = str(task_dir.relative_to(self.source))
            if (
                self.task_ids is not None
                and slug not in self.task_ids
                and rel not in self.task_ids
            ):
                continue
            selected.append(task_dir)
        selected.sort(key=lambda path: str(path.relative_to(self.source)))
        if self.limit is not None:
            selected = selected[: self.limit]
        slugs = _unique_slugs(selected, self.source)

        self.output_dir.mkdir(parents=True, exist_ok=True)
        for task_dir in selected:
            self._emit(task_dir, authors, slugs[task_dir])
        exclusions = self.output_dir / f"EXCLUSIONS-{self.surface}.md"
        if self.surface == "survey":
            exclusions = self.output_dir / "EXCLUSIONS.md"
        lines = [f"# Excluded {self.surface} tasks", ""]
        lines.extend(f"- {item}" for item in self.excluded)
        if not self.excluded:
            lines.append("None.")
        exclusions.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.info(
            "generated %s %s tasks, excluded %s",
            self.generated_count,
            self.surface,
            len(self.excluded),
        )

    def _emit_survey(self, task_dir: Path, authors: list[str], slug: str) -> None:
        dest = self.output_dir / slug
        if dest.exists():
            if not self.overwrite:
                logger.info("skip existing %s", slug)
                return
            shutil.rmtree(dest)

        source_input = task_dir / "input"
        verifier = task_dir / "tests" / "verifier.py"
        persona = task_dir / "persona.yaml"
        instruction = task_dir / "instruction.md"
        if not source_input.is_dir() or not verifier.is_file():
            self.excluded.append(f"{task_dir.name}: missing input/ or verifier")
            return
        if not persona.is_file() or not instruction.is_file():
            self.excluded.append(f"{task_dir.name}: missing persona.yaml or instruction.md")
            return
        try:
            script = solve_script(task_dir)
            verifier_text, checks_toml = _verifier_for_harbor(task_dir, verifier)
            timeouts = _timeouts(task_dir)
        except OracleError as exc:
            self.excluded.append(f"{task_dir.name}: {exc}")
            if dest.exists():
                shutil.rmtree(dest)
            return

        (dest / "solution").mkdir(parents=True)
        (dest / "tests").mkdir()
        shutil.copytree(source_input, dest / "environment" / "input")
        (dest / "environment" / "Dockerfile").write_text(_DOCKERFILE, encoding="utf-8")
        shutil.copytree(source_input, dest / "input")
        shutil.copy2(instruction, dest / "instruction.md")
        shutil.copy2(persona, dest / "persona.yaml")
        (dest / "tests" / "verifier.py").write_text(verifier_text, encoding="utf-8")
        if checks_toml is not None:
            (dest / "tests" / "checks.toml").write_text(checks_toml, encoding="utf-8")
        check_json = task_dir / "tests" / "check.json"
        if check_json.is_file():
            shutil.copy2(check_json, dest / "tests" / "check.json")
        for key_name in ("answer_key.yaml", "answer_key.json", "questionnaire.yaml"):
            key = task_dir / "tests" / key_name
            if key.is_file():
                shutil.copy2(key, dest / "tests" / key_name)
        shutil.copytree(source_input, dest / "tests" / "input")
        (dest / "tests" / "test.sh").write_text(_TEST_SH, encoding="utf-8")
        (dest / "solution" / "solve.sh").write_text(script, encoding="utf-8")
        (dest / "task.toml").write_text(
            _task_toml(slug, authors, timeouts),
            encoding="utf-8",
        )
        self.generated_count += 1

    def _emit(self, task_dir: Path, authors: list[str], slug: str) -> None:
        if self.surface == "survey":
            self._emit_survey(task_dir, authors, slug)
            return
        if self.surface == "chat":
            self._emit_chat(task_dir, authors, slug)
            return
        self._emit_interactive(task_dir, authors, slug)

    def _emit_chat(self, task_dir: Path, authors: list[str], slug: str) -> None:
        dest = self.output_dir / slug
        if dest.exists():
            if not self.overwrite:
                logger.info("skip existing %s", slug)
                return
            shutil.rmtree(dest)

        verifier = task_dir / "tests" / "verifier.py"
        persona = task_dir / "persona.yaml"
        instruction = task_dir / "instruction.md"
        source_input = task_dir / "input"
        if not verifier.is_file() or not persona.is_file() or not instruction.is_file():
            self.excluded.append(f"{slug}: missing verifier, persona.yaml, or instruction.md")
            return
        if not (source_input / "bot.md").is_file():
            self.excluded.append(f"{slug}: missing input/bot.md")
            return
        try:
            script = chat_solve_script(task_dir, slug)
            verifier_text, checks_toml = _verifier_for_harbor(task_dir, verifier)
            timeouts = _timeouts(task_dir)
            limits = _limits(task_dir, self.surface)
            healthcheck = _healthcheck(task_dir)
            application_id = _application_id(task_dir)
        except OracleError as exc:
            self.excluded.append(f"{slug}: {exc}")
            if dest.exists():
                shutil.rmtree(dest)
            return

        env = dest / "environment"
        shutil.copytree(
            source_input,
            env / "input",
            ignore=shutil.ignore_patterns("persona.yaml"),
        )
        (env / "Dockerfile").write_text(_CHAT_DOCKERFILE, encoding="utf-8")
        tests = dest / "tests"
        tests.mkdir(parents=True)
        for path in (task_dir / "tests").iterdir():
            if path.is_file() and path.name != "test.sh":
                shutil.copy2(path, tests / path.name)
        (tests / "verifier.py").write_text(verifier_text, encoding="utf-8")
        if checks_toml is not None:
            (tests / "checks.toml").write_text(checks_toml, encoding="utf-8")
        (tests / "llm_client.py").write_text(
            _judge_client_source().read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (tests / "test.sh").write_text(_CHAT_TEST_SH, encoding="utf-8")
        shutil.copytree(
            source_input,
            tests / "input",
            ignore=shutil.ignore_patterns("persona.yaml"),
        )
        shutil.copy2(instruction, tests / "instruction.md")
        shutil.copy2(persona, tests / "persona.yaml")
        shutil.copy2(instruction, dest / "instruction.md")
        shutil.copy2(persona, dest / "persona.yaml")
        solution = dest / "solution"
        solution.mkdir()
        (solution / "solve.sh").write_text(script, encoding="utf-8")
        (dest / "task.toml").write_text(
            _task_toml(
                slug,
                authors,
                timeouts,
                surface=self.surface,
                limits=limits,
                healthcheck=healthcheck,
                application_id=application_id,
            ),
            encoding="utf-8",
        )
        self.generated_count += 1

    def _emit_interactive(self, task_dir: Path, authors: list[str], slug: str) -> None:
        dest = self.output_dir / slug
        if dest.exists():
            if not self.overwrite:
                logger.info("skip existing %s", slug)
                return
            shutil.rmtree(dest)

        verifier = task_dir / "tests" / "verifier.py"
        persona = task_dir / "persona.yaml"
        instruction = task_dir / "instruction.md"
        if not verifier.is_file() or not persona.is_file() or not instruction.is_file():
            self.excluded.append(f"{slug}: missing verifier, persona.yaml, or instruction.md")
            return
        try:
            script = solve_script(task_dir)
            verifier_text, checks_toml = _verifier_for_harbor(task_dir, verifier)
            if self.surface == "web":
                self._web_environment(task_dir, dest / "environment")
            else:
                self._app_environment(task_dir, dest / "environment")
            timeouts = _timeouts(task_dir)
            limits = _limits(task_dir, self.surface)
            healthcheck = _healthcheck(task_dir)
        except OracleError as exc:
            self.excluded.append(f"{slug}: {exc}")
            if dest.exists():
                shutil.rmtree(dest)
            return

        tests = dest / "tests"
        tests.mkdir(parents=True)
        for path in (task_dir / "tests").iterdir():
            if path.is_file() and path.name != "test.sh":
                shutil.copy2(path, tests / path.name)
        (tests / "verifier.py").write_text(verifier_text, encoding="utf-8")
        uses_judge = "llm_client" in verifier_text
        if uses_judge:
            shutil.copy2(_judge_client_source(), tests / "llm_client.py")
        if checks_toml is not None:
            (tests / "checks.toml").write_text(checks_toml, encoding="utf-8")
        (tests / "test.sh").write_text(_INTERACTIVE_TEST_SH, encoding="utf-8")
        if (dest / "environment" / "input").is_dir():
            shutil.copytree(dest / "environment" / "input", tests / "input")
        shutil.copytree(dest / "environment", tests / "environment")
        shutil.copy2(instruction, tests / "instruction.md")
        shutil.copy2(persona, tests / "persona.yaml")
        shutil.copy2(instruction, dest / "instruction.md")
        shutil.copy2(persona, dest / "persona.yaml")
        solution = dest / "solution"
        solution.mkdir()
        (solution / "solve.sh").write_text(script, encoding="utf-8")
        web_spec = task_dir / "solution" / "web.json"
        if web_spec.is_file():
            shutil.copy2(web_spec, solution / "web.json")
        (dest / "task.toml").write_text(
            _task_toml(
                slug,
                authors,
                timeouts,
                surface=self.surface,
                limits=limits,
                healthcheck=healthcheck,
                uses_judge=uses_judge,
            ),
            encoding="utf-8",
        )
        self.generated_count += 1

    def _web_environment(self, task_dir: Path, env: Path) -> None:
        base = _base_dockerfile(self.source, _WEB_BASE)
        if "pyyaml" not in base:
            base = base.rstrip() + "\nRUN pip install --no-cache-dir pyyaml==6.0.2\n"
        source_input = task_dir / "input"
        if not source_input.is_dir():
            raise OracleError("web task has no input/")
        shutil.copytree(source_input, env / "input")
        lines = ["RUN mkdir -p /app/output /app/input", "COPY input/ /app/input/"]
        if (source_input / "site").is_dir():
            lines.append("COPY input/site /app/site")
        web_spec = task_dir / "solution" / "web.json"
        if web_spec.is_file():
            shutil.copy2(web_spec, env / "web.json")
            lines.append("COPY web.json /app/web.json")
        (env / "Dockerfile").write_text(base.rstrip() + "\n" + "\n".join(lines) + "\n", encoding="utf-8")

    def _app_environment(self, task_dir: Path, env: Path) -> None:
        upstream = task_dir / "environment"
        if not (upstream / "Dockerfile").is_file():
            raise OracleError("app task has no environment/Dockerfile")
        shutil.copytree(upstream, env)
        source_input = task_dir / "input"
        if source_input.is_dir():
            shutil.copytree(source_input, env / "input")
        kept = []
        for line in (upstream / "Dockerfile").read_text(encoding="utf-8").splitlines():
            if line.startswith("FROM ") or line.startswith("# syntax"):
                continue
            kept.append(line)
        extra = [
            "RUN apt-get update && apt-get install -y --no-install-recommends python3-yaml \\",
            "    && rm -rf /var/lib/apt/lists/*",
            "RUN mkdir -p /app/output /app/input",
        ]
        if (env / "input").is_dir():
            extra.append("COPY input/ /app/input/")
        extra.append("COPY . /opt/task-environment/")
        base = _base_dockerfile(self.source, _APP_BASE).rstrip()
        body = "\n".join(kept + extra)
        (env / "Dockerfile").write_text(base + "\n" + body + "\n", encoding="utf-8")


def _base_dockerfile(source: Path, relative: str) -> str:
    path = source / relative
    if not path.is_file():
        raise OracleError(f"missing shared environment {path}")
    return path.read_text(encoding="utf-8")


def _tasks_for(source: Path, surface: str) -> list[Path]:
    if surface == "survey":
        return _survey_tasks(source)
    suffix = {"web": "-web", "app": "-app", "chat": "-chat"}[surface]
    return [path.parent.parent for path in (source / "tasks").glob(f"**/*{suffix}/tests/verifier.py")]


def _survey_tasks(source: Path) -> list[Path]:
    tasks = []
    for verifier in (source / "tasks").glob("**/*-survey/tests/verifier.py"):
        tasks.append(verifier.parents[1])
    return tasks


def _registry_slug(name: str) -> str:
    slug = _SLUG_RE.sub("-", name.lower()).strip("-")
    return slug or "task"


def _unique_slugs(selected: list[Path], source: Path) -> dict[Path, str]:
    """Keep the task directory name unless another task already uses it."""
    used: set[str] = set()
    slugs: dict[Path, str] = {}
    for task_dir in selected:
        parts = [task_dir.name]
        cursor = task_dir.parent
        slug = _registry_slug(task_dir.name)
        while slug in used and cursor != source:
            parts.append(cursor.name)
            slug = _registry_slug("-".join(reversed(parts)))
            cursor = cursor.parent
        used.add(slug)
        slugs[task_dir] = slug
    return slugs


def _read_task_text(task_dir: Path) -> str:
    path = task_dir / "task.toml"
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise OracleError(f"cannot read task.toml: {exc}") from exc


def _read_task_data(task_dir: Path) -> dict:
    try:
        data = tomllib.loads(_read_task_text(task_dir))
    except tomllib.TOMLDecodeError as exc:
        raise OracleError(f"cannot parse task.toml: {exc}") from exc
    if not isinstance(data, dict):
        raise OracleError("task.toml is not a table")
    return data


def _timeouts(task_dir: Path) -> tuple[float, float]:
    text = _read_task_text(task_dir)
    agent = _timeout(text, "agent") or 300.0
    verifier = _timeout(text, "verifier") or 30.0
    return agent, verifier


def _timeout(text: str, section: str) -> float | None:
    match = re.search(rf"\[{section}\](.*?)(?=\n\[|\Z)", text, re.S)
    if match is None:
        return None
    value = re.search(r"timeout_sec\s*=\s*([0-9.]+)", match.group(1))
    if value is None:
        return None
    return float(value.group(1))


def _citation_authors(source: Path) -> list[str]:
    path = source / "CITATION.cff"
    if not path.is_file():
        return ["AgentPersonaBench authors"]
    names: list[str] = []
    given = family = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("  - family-names:"):
            if given or family:
                names.append(_join_name(given, family))
            family = line.split(":", 1)[1].strip().strip('"')
            given = None
        elif line.startswith("    given-names:") and family is not None:
            given = line.split(":", 1)[1].strip().strip('"')
            names.append(_join_name(given, family))
            given = family = None
    if names:
        return names
    return ["AgentPersonaBench authors"]


def _join_name(given: str | None, family: str | None) -> str:
    return " ".join(part for part in (given, family) if part)


def _verifier_for_harbor(task_dir: Path, verifier: Path) -> tuple[str, str | None]:
    """Point task.toml reads at tests/checks.toml.

    Harbor's task.toml is only the harness config. Verifiers that loaded
    ``[[checks]]`` from the benchmark task.toml keep those thresholds in
    ``tests/checks.toml``, beside the verifier.
    """
    text = verifier.read_text(encoding="utf-8")
    if _TASK_TOML_READ.search(text) is None:
        if _OTHER_TASK_TOML_READ.search(text) and "checks.toml" not in text:
            raise OracleError("verifier reads task.toml through an unhandled pattern")
        return text, None
    checks = _checks_toml(task_dir)
    if checks is None:
        raise OracleError("verifier reads task.toml but it has no [[checks]]")
    rewritten = _TASK_TOML_READ.sub(_CHECKS_PATH, text)
    if _TASK_TOML_READ.search(rewritten):
        raise OracleError("verifier still reads task.toml after the checks.toml rewrite")
    return rewritten, checks


def _checks_toml(task_dir: Path) -> str | None:
    data = _read_task_data(task_dir)
    checks = data.get("checks")
    if not isinstance(checks, list) or not checks:
        return None
    lines: list[str] = []
    for check in checks:
        if not isinstance(check, dict):
            raise OracleError("[[checks]] entry is not a table")
        lines.append("[[checks]]")
        for key, value in check.items():
            lines.append(f"{key} = {_toml_value(value)}")
        lines.append("")
    return "\n".join(lines)


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return f'"{_toml_str(value)}"'
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise OracleError(f"cannot write check value of type {type(value).__name__}")


def _limits(task_dir: Path, surface: str) -> tuple[int, int, int, float]:
    data = _read_task_data(task_dir)
    env = data.get("environment") if isinstance(data.get("environment"), dict) else {}
    defaults = {
        "survey": (1, 2048, 10240, 600.0),
        "web": (1, 2048, 10240, 900.0),
        "app": (4, 4096, 40960, 900.0),
        "chat": (1, 2048, 10240, 600.0),
    }
    cpus, memory, storage, build = defaults[surface]
    return (
        int(env.get("cpus") or cpus),
        int(env.get("memory_mb") or memory),
        int(env.get("storage_mb") or storage),
        float(env.get("build_timeout_sec") or build),
    )


def _healthcheck(task_dir: Path) -> dict | None:
    data = _read_task_data(task_dir)
    env = data.get("environment") if isinstance(data.get("environment"), dict) else {}
    check = env.get("healthcheck")
    if not isinstance(check, dict) or not isinstance(check.get("command"), str):
        return None
    return check


def _application_id(task_dir: Path) -> str | None:
    data = _read_task_data(task_dir)
    meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    value = meta.get("application_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _judge_client_source() -> Path:
    return Path(__file__).resolve().parent / "judge_client.py"


def _task_toml(
    slug: str,
    authors: list[str],
    timeouts: tuple[float, float],
    surface: str = "survey",
    limits: tuple[int, int, int, float] | None = None,
    healthcheck: dict | None = None,
    application_id: str | None = None,
    uses_judge: bool = False,
) -> str:
    agent_timeout, verifier_timeout = timeouts
    cpus, memory, storage, build = limits or (1, 2048, 10240, 600.0)
    author_lines = ",\n".join(f'    {{ name = "{_toml_str(name)}" }}' for name in authors)
    app_line = ""
    if application_id:
        app_line = f'\napplication_id = "{_toml_str(application_id)}"'
    verifier_env = ""
    if surface == "chat" or uses_judge:
        verifier_env = """
[verifier.env]
ANTHROPIC_API_KEY = "${ANTHROPIC_API_KEY}"
ADHERENCE_JUDGE_MODEL = "claude-opus-4-8"
LLM_REASONING_EFFORT = "medium"
"""
    check = ""
    if healthcheck is not None:
        timeout = float(healthcheck.get("timeout_sec") or 60.0)
        retries = int(healthcheck.get("retries") or 5)
        check = f"""
[environment.healthcheck]
command = "{_toml_str(str(healthcheck["command"]))}"
timeout_sec = {timeout}
retries = {retries}
"""
    return f"""schema_version = "1.0"

[task]
name = "agentpersonabench/{surface}-{slug}"
authors = [
{author_lines}
]
keywords = ["agentpersonabench", "{surface}", "persona"]

[metadata]
surface = "{surface}"
source_slug = "{slug}"{app_line}

[verifier]
timeout_sec = {verifier_timeout}
{verifier_env}
[agent]
timeout_sec = {agent_timeout}

[environment]
build_timeout_sec = {build}
cpus = {cpus}
memory_mb = {memory}
storage_mb = {storage}
{check}"""


def _toml_str(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
