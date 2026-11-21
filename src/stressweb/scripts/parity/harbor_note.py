"""Host-side Harbor custom agent using the pinned upstream model policy."""
import base64
import json
import os
from pathlib import Path
import shlex
import tempfile
from types import SimpleNamespace

from harbor.agents.base import BaseAgent
from pinned import load_upstream


class StressWebNoteAgent(BaseAgent):
    @staticmethod
    def name():
        return "stressweb-note"

    def version(self):
        return "be52dbf"

    async def setup(self, environment):
        self.policy_class = load_upstream()
        if self.model_name != os.environ["STRESSWEB_MODEL"]:
            raise ValueError("Harbor's model must match STRESSWEB_MODEL exactly")

    async def observe(self, environment, local):
        # Use file transfer for the screenshots and DOM: CLI stdout may be capped.
        result = await environment.exec(
            "browser observe > /tmp/note-observation.json && "
            "browser screenshot /tmp/note-observation.png", timeout_sec=90)
        if result.return_code:
            raise RuntimeError(f"Browser observation failed: {result.stderr}")
        await environment.download_file("/tmp/note-observation.json", local/"observation.json")
        await environment.download_file("/tmp/note-observation.png", local/"screen.png")
        observation = json.loads((local/"observation.json").read_text())
        observation["screenshot"] = base64.b64encode((local/"screen.png").read_bytes()).decode()
        return observation

    async def run(self, instruction, environment, context):
        query = instruction.strip().splitlines()[0]
        viewport = SimpleNamespace(viewport_width=1920, viewport_height=1080)
        policy = self.policy_class(viewport, self.model_name, max_steps=100, use_dom=True,
                                   result_dir=str(self.logs_dir))
        parse_failures = 0
        with tempfile.TemporaryDirectory(dir=self.logs_dir) as tmp:
            local = Path(tmp)
            while policy.current_step < policy.max_steps:
                observation = await self.observe(environment, local)
                policy.observation_history.append(observation)
                action = await policy.get_next_action(observation, query)
                kind = action["action_type"]
                if kind == "PARSE_ERROR":
                    parse_failures += 1
                    if parse_failures < 3:
                        continue
                    break
                parse_failures = 0
                if kind == "FAIL" and action.get("parameters", {}).get("reason", "").startswith("API call failed:"):
                    raise RuntimeError("Model API failed; see the upstream run output")
                command = "browser act " + shlex.quote(json.dumps(action))
                response = await environment.exec(command, timeout_sec=120)
                try:
                    result = json.loads(response.stdout or "")
                except ValueError as exc:
                    raise RuntimeError(f"Browser transport failed: {response.stderr}") from exc
                policy.action_history.append({"step":policy.current_step, "action":action, "result":result})
                policy.current_step += 1
                context.metadata = {"steps":policy.current_step, "last_action":kind}
                (self.logs_dir/"actions.json").write_text(json.dumps(policy.action_history))
                if kind in {"DONE","FAIL"}:
                    break
