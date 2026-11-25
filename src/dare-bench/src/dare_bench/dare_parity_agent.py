"""Port of DARE-Bench's original agent loop for matched parity runs.

Mirrors upstream scripts/run_agentic_reason.py + llm_serving/remote_llm.py + tools/python_executor
at the settings in scripts/run_parallel_datasci_auto.sh and config/datasci.yaml:

- the user message is the original question, verbatim; no system prompt, no tool hint;
- one tool, python_executor (stateless runs, explicit files_to_load / files_to_save,
  200 s limit, 200 MB file limit), described exactly as upstream describes it;
- up to max_turn = 5 model generations; the episode ends at the first generation with no
  tool call.

Two request paths, chosen by model name exactly as upstream's agentic_reason/models.py does
(with the harbor-parity fork's GPT-6 alias):

- gpt-5 / gpt-6*: OpenAIResponsesClient. Responses API with max_output_tokens 16384,
  reasoning {effort: high, summary: auto}, text {verbosity: medium}, no temperature/top_p. Every
  output item is appended to the input via to_dict(), then one function_call_output per call.
  A failed tool call is sent back as json.dumps({"Error": message}).
- other gpt models: OpenAIChatCompletionClient. Chat completions with max_tokens 16384,
  top_p 0.8 and temperature 0.7 (the script passes 0.0, but run_generation() does
  `temperature or 0.7`). A failed tool call is sent back as the bare message.

Tool errors (missing or unsafe file, file over the size limit, wrong argument types, unknown tool)
are returned to the model with upstream's exact messages and the episode continues. Cases where
upstream's harness itself raises end the episode and remove /workspace/prediction.csv, because
upstream then records no result and the run scores 0:
- chat path: tool-call arguments that are not valid JSON (upstream's error handler reads a
  missing attribute) or valid JSON that is not an object;
- Responses path: arguments that are valid JSON but not an object, a message item with more than
  one content part, or an output item type upstream does not handle;
- any error from the model API (upstream has no retry on these paths).

Usage:
  harbor run -p <dataset> -a dare_bench.dare_parity_agent:DAREBenchParityAgent -m openai/<model>
with OPENAI_API_KEY (and OPENAI_BASE_URL for a compatible endpoint) set, and
PYTHONPATH including this adapter's src/ directory.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from pathlib import Path

from openai import OpenAI

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

WORK_DIR = "/workspace"
PRIVATE_DIR = "/opt/dare_parity"  # root-only: executor, code and request files
EXECUTOR_PATH = f"{PRIVATE_DIR}/sandbox_executor.py"
SANDBOX_USER = "dare_sandbox"
SETUP_SCRIPT = f"""set -e
test "$(id -u)" = 0
id -u {SANDBOX_USER} >/dev/null 2>&1 || useradd --system --create-home --home-dir /home/{SANDBOX_USER} --shell /usr/sbin/nologin {SANDBOX_USER}
mkdir -p {PRIVATE_DIR}
chmod 700 {PRIVATE_DIR} {WORK_DIR}
# SandboxFusion runs code as root, so paths such as /mnt/data are writable there; keep them writable here.
mkdir -p /mnt
chmod 1777 /mnt
"""

TOOL_DESCRIPTION = (
    "Execute a Python script in an isolated HTTP sandbox with a {timeout}-second time limit. "
    "Each run is single-shot and stateless (no REPL, no persistent environment between runs). "
    "You may upload input files via `files_to_load` and retrieve results via `files_to_save`. "
    "The maximum file size for both upload and download is {max_file_size_mb} MB. "
    "The tool returns the full program output, including both stdout and stderr. "
    "Use explicit `print(...)` statements to capture values in the output. "
    "This tool can be invoked up to {max_turns} times per conversation."
)
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "code": {"type": "string", "description": "The Python code to execute."},
        "files_to_load": {
            "type": "array",
            "items": {"type": "string"},
            "description": "List of input file paths to upload prior to execution (e.g. [\"input1.csv\", \"config.json\"]).",
        },
        "files_to_save": {
            "type": "array",
            "items": {"type": "string"},
            "description": "List of output file paths to download after execution (e.g. [\"results.csv\", \"log.txt\"]).",
        },
    },
    "required": ["code", "files_to_load", "files_to_save"],
}


class HarnessCrash(Exception):
    """A condition under which upstream's harness raises; the run records no result there."""


def original_question(instruction: str) -> str:
    """Strip the adapter's one-paragraph environment preamble; return the question verbatim."""
    return instruction.split("\n\n", 1)[1].strip() if "\n\n" in instruction else instruction


RESPONSES_MODEL_IDS = {"gpt-o4-mini": "o4-mini", "gpt-o3": "o3"}


def uses_responses_api(model: str) -> bool:
    """Upstream routes these names to OpenAIResponsesClient (agentic_reason/models.py)."""
    return model in ("gpt-o4-mini", "gpt-o3", "gpt-5") or model.startswith("gpt-6")


def missing_args_message(missing: list[str]) -> str:
    """Python's TypeError text for upstream's validate_inputs wrapper called without arguments."""
    quoted = [repr(name) for name in missing]
    joined = quoted[0] if len(quoted) == 1 else ", ".join(quoted[:-1]) + " and " + quoted[-1]
    plural = "argument" if len(missing) == 1 else "arguments"
    return f"validate_inputs.<locals>.wrapper() missing {len(missing)} required positional {plural}: {joined}"


class DAREBenchParityAgent(BaseAgent):
    def __init__(
        self,
        *args,
        max_turn: int = 5,
        tool_timeout: int = 200,
        max_file_size_mb: int = 200,
        temperature: float = 0.7,
        top_p: float = 0.8,
        max_tokens: int = 16384,
        reasoning_effort: str = "high",
        reasoning_summary: str = "auto",
        text_verbosity: str = "medium",
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.max_turn = int(max_turn)
        self.tool_timeout = int(tool_timeout)
        self.max_file_size = int(max_file_size_mb) * 1024 * 1024
        self.max_file_size_mb = int(max_file_size_mb)
        self.temperature = float(temperature)
        self.top_p = float(top_p)
        self.max_tokens = int(max_tokens)
        self.reasoning_effort = reasoning_effort
        self.reasoning_summary = reasoning_summary
        self.text_verbosity = text_verbosity

    @staticmethod
    def name() -> str:
        return "dare-bench-parity-agent"

    def version(self) -> str | None:
        return "1.3.0"

    async def setup(self, environment: BaseEnvironment) -> None:
        # The original runs model code in a separate SandboxFusion container that holds only the
        # files listed in files_to_load. Here the code runs as an unprivileged user that cannot read
        # /workspace, so it cannot reach the data or write prediction.csv except via the tool.
        result = await environment.exec(SETUP_SCRIPT, timeout_sec=120)
        if result.return_code != 0:
            raise RuntimeError(f"sandbox setup failed (needs root): {result.stdout} {result.stderr}")
        await environment.upload_file(Path(__file__).with_name("sandbox_executor.py"), EXECUTOR_PATH)

    def tool_description(self) -> str:
        return TOOL_DESCRIPTION.format(
            timeout=self.tool_timeout, max_turns=self.max_turn, max_file_size_mb=self.max_file_size_mb
        )

    def chat_tool_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "python_executor",
                "description": self.tool_description(),
                "parameters": {**TOOL_PARAMETERS, "additionalProperties": False},
                "strict": True,
            },
        }

    def responses_tool_schema(self) -> dict:
        return {
            "type": "function",
            "name": "python_executor",
            "description": self.tool_description(),
            "parameters": {**TOOL_PARAMETERS, "additionalProperties": False},
            "strict": True,
        }

    async def _file_size(self, environment: BaseEnvironment, name: str) -> int | None:
        result = await environment.exec(f"stat -c %s {WORK_DIR}/{name} 2>/dev/null || echo missing", timeout_sec=30)
        text = (result.stdout or "").strip()
        return None if text == "missing" or not text.isdigit() else int(text)

    async def python_executor(self, environment: BaseEnvironment, args: dict) -> tuple[str, bool]:
        """Upstream python_executor.__call__ behind dispatch_tool_call: returns (output, success)."""
        missing = [key for key in ("code", "files_to_load", "files_to_save") if key not in args]
        if missing:
            return f"Function call failed: {missing_args_message(missing)}", False
        code, files_to_load, files_to_save = args["code"], args["files_to_load"], args["files_to_save"]
        if not isinstance(files_to_load, list):
            return "Function call failed: files_to_load must be a list", False
        if not isinstance(files_to_save, list):
            return "Function call failed: files_to_save must be a list", False
        if not isinstance(code, str):
            return "Function call failed: code must be a string", False
        runtime_error = "Tool runtime error in python_executor: "
        for name in files_to_load:
            if os.path.basename(name) != name:
                return f"{runtime_error}Invalid file path: {name!r}. Must be a direct child of work_dir.", False
            size = await self._file_size(environment, name)
            if size is None:
                return f"{runtime_error}File not found: {name}", False
            if size > self.max_file_size:
                limit_mb, actual_mb = self.max_file_size / (1024 * 1024), size / (1024 * 1024)
                return (
                    f"{runtime_error}File {name} ({actual_mb:.2f}MB) exceeds maximum size limit of {limit_mb:.2f}MB",
                    False,
                )

        call_id = uuid.uuid4().hex
        remote_code, remote_request = f"{PRIVATE_DIR}/code_{call_id}.py", f"{PRIVATE_DIR}/request_{call_id}.json"
        request = {
            "code_path": remote_code,
            "files_to_load": files_to_load,
            "files_to_save": files_to_save,
            "work_dir": WORK_DIR,
            "timeout": self.tool_timeout,
            "run_as": SANDBOX_USER,
        }
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "code.py").write_text(code)
            (Path(tmp) / "request.json").write_text(json.dumps(request))
            await environment.upload_file(Path(tmp) / "code.py", remote_code)
            await environment.upload_file(Path(tmp) / "request.json", remote_request)
        result = await environment.exec(
            f"python3 {EXECUTOR_PATH} {remote_request}", timeout_sec=self.tool_timeout + 120
        )
        lines = (result.stdout or "").strip().splitlines()
        if result.return_code != 0 or not lines:
            # The sandbox service itself failed; upstream returns such failures as an error dict.
            return {"stdout": "", "stderr": f"Error: executor failed: {(result.stderr or '')[-2000:]}"}, False
        output = lines[-1]
        error = json.loads(output).get("error") if output.startswith('{"error"') else None
        if error is not None:
            return f"{runtime_error}{error}", False
        return output, True

    async def dispatch(self, environment: BaseEnvironment, name: str, arguments: str, chat: bool) -> tuple:
        """Upstream dispatch_tool_call for the chat (chat=True) or Responses client."""
        if name != "python_executor":
            if chat:
                return f"\nError: Unsupported tool call: {name}. Please check the tool name carefully.\n", False
            return f"Unsupported tool call: {name}.\n", False
        try:
            args = json.loads(arguments)
        except json.JSONDecodeError as exc:
            if chat:
                raise HarnessCrash(f"invalid tool-call JSON on the chat path: {exc}") from exc
            return f"Json decode error for tool call: {arguments}", False
        if not isinstance(args, dict):
            raise HarnessCrash("tool-call arguments are not a JSON object")
        return await self.python_executor(environment, args)

    async def run_chat(self, client, model, instruction, environment, usage) -> tuple[list, int]:
        messages: list[dict] = [{"role": "user", "content": original_question(instruction)}]
        self._transcript = messages
        tools = [self.chat_tool_schema()]
        for turn in range(1, self.max_turn + 1):
            response = await asyncio.to_thread(
                client.chat.completions.create,
                model=model,
                messages=messages,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                top_p=self.top_p,
                stop=[],
                n=1,
                tools=tools,
            )
            if response.usage:
                usage["input"] += response.usage.prompt_tokens or 0
                usage["output"] += response.usage.completion_tokens or 0
            message = response.choices[0].message
            dumped = message.model_dump()
            # Newer openai clients add a null "annotations" field the original harness's client omits.
            if dumped.get("annotations") is None:
                dumped.pop("annotations", None)
            messages.append(dumped)
            if not message.tool_calls:
                return "finished", turn
            for call in message.tool_calls:
                result, _ = await self.dispatch(environment, call.function.name, call.function.arguments, chat=True)
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "name": call.function.name, "content": str(result)}
                )
        return "max_turn_reached", self.max_turn

    async def run_responses(self, client, model, instruction, environment, usage) -> tuple[str, int]:
        items: list[dict] = [{"role": "user", "content": original_question(instruction)}]
        self._transcript = items
        request = {
            # OpenAIResponsesClient._init_client sends gpt-o4-mini / gpt-o3 as o4-mini / o3.
            "model": RESPONSES_MODEL_IDS.get(model, model),
            "max_output_tokens": self.max_tokens,
            "reasoning": {"effort": self.reasoning_effort, "summary": self.reasoning_summary},
        }
        if model == "gpt-5" or model.startswith("gpt-6"):
            verbosity = (self.text_verbosity or "medium").lower()
            request["text"] = {"verbosity": verbosity if verbosity in {"low", "medium", "high"} else "medium"}
        tools = [self.responses_tool_schema()]
        for turn in range(1, self.max_turn + 1):
            response = await asyncio.to_thread(
                client.responses.create,
                model=request["model"],
                input=items,
                max_output_tokens=request["max_output_tokens"],
                reasoning=request["reasoning"],
                **({"text": request["text"]} if "text" in request else {}),
                tools=tools,
            )
            if response.usage:
                usage["input"] += response.usage.input_tokens or 0
                usage["output"] += response.usage.output_tokens or 0
            out = response.output
            for block in out:  # upstream log_response
                if block.type == "message" and len(block.content) != 1:
                    raise HarnessCrash(f"message item with {len(block.content)} content parts")
                if block.type not in ("reasoning", "function_call", "message"):
                    raise HarnessCrash(f"unhandled output item type {block.type}")
            for block in out:
                items.append(block.to_dict())
            calls = [block for block in out if block.type == "function_call"]
            if not calls:
                return "finished", turn
            for call in calls:
                result, success = await self.dispatch(environment, call.name, call.arguments, chat=False)
                if not success:
                    result = json.dumps({"Error": result})
                items.append({"type": "function_call_output", "call_id": call.call_id, "output": result})
        return "max_turn_reached", self.max_turn

    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        model = self.model_name.split("/", 1)[1] if self.model_name and "/" in self.model_name else self.model_name
        if not model:
            raise RuntimeError("a model name is required, e.g. -m openai/gpt-6.1-sol")
        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], base_url=os.environ.get("OPENAI_BASE_URL") or None)
        api = "responses" if uses_responses_api(model) else "chat"
        usage = {"input": 0, "output": 0}
        self._transcript: list = []
        status, turns = "not_started", 0
        try:
            loop = self.run_responses if api == "responses" else self.run_chat
            status, turns = await loop(client, model, instruction, environment, usage)
        except Exception as exc:  # upstream records no result for any harness or API exception
            status = f"crash: {type(exc).__name__}: {exc}"[:2000]
            await environment.exec(f"rm -f {WORK_DIR}/prediction.csv", timeout_sec=30)
        finally:
            context.n_input_tokens = usage["input"]
            context.n_output_tokens = usage["output"]
            context.metadata = {"status": status, "turns": turns, "model": model, "api": api}
            (self.logs_dir / "dare_messages.json").write_text(json.dumps(self._transcript, indent=1, default=str))
