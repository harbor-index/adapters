"""Run with the pinned *upstream* Python environment, on staged task paths."""
from pathlib import Path
import json
import sys
import shlex
from unittest.mock import Mock
from loopsbench.agents.codex.codex_agent import CodexAgent
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from loopsbench.agents.agent_name import AgentName
from loopsbench.handlers.trial_handler import Task
from loopsbench.harness.harness import Harness
from loopsbench.harness.instruction_composer import compose_instruction

if __name__ == "__main__":
    capture = "--capture" in sys.argv
    if capture:
        sys.argv.remove("--capture")
    harness = object.__new__(Harness)
    prompts = []
    with TemporaryDirectory() as tmp:
        for name in sys.argv[1:]:
            directory = Path(name)
            handler = SimpleNamespace(task_dir=directory, trial_paths=SimpleNamespace(
                requirement_patches_path=Path(tmp) / "fresh-trial"))
            task = Task.from_yaml(directory / "task.yaml")
            base = compose_instruction(task.instruction, AgentName.CODEX)
            prompt = harness._build_progress_instruction(base, harness._progress_state(handler))
            if capture:
                session = Mock()
                CodexAgent.perform_task(object.__new__(CodexAgent), prompt, session)
                argv = shlex.split(session.send_keys.call_args.args[0])
                prompt = argv[argv.index("-o") + 2]
            prompts.append(prompt)
    print(json.dumps(prompts))
