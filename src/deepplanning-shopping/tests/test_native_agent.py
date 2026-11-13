import asyncio
import copy
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from harbor.environments.base import ExecResult
from harbor.models.agent.context import AgentContext

from deepplanning_shopping.native_agent import ShoppingFnAgent, load_model_config


@pytest.fixture
def model_config(tmp_path):
    config = {
        "model_name": "offline-model",
        "model_type": "openai",
        "base_url": "http://127.0.0.1:1/v1",
        "api_key_env": "DEEPPLANNING_TEST_KEY",
        "max_retries": 1,
        "temperature": 0.4,
        "extra_body": {"max_tokens": 128},
    }
    path = tmp_path / "models_config.json"
    path.write_text(json.dumps({"models": {"offline-model": config}}))
    return path


@pytest.fixture
def runtime(tmp_path, generated, monkeypatch):
    root = tmp_path / "runtime"
    shutil.copytree(generated / "shopping-level1-case-001/environment", root)
    before = dict(sys.modules)
    # The upstream tools use absolute imports. Each test gets its own runtime.
    for name in list(sys.modules):
        if name.startswith(
            ("native_agent", "tools", "base_shopping_tool", "shopping_")
        ):
            sys.modules.pop(name)
    old_path = list(sys.path)
    monkeypatch.syspath_prepend(str(root))
    spec = importlib.util.spec_from_file_location("run_native", root / "run_native.py")
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    yield root, entry
    sys.path[:] = old_path
    for name in set(sys.modules) - set(before):
        if name.startswith(
            ("native_agent", "tools", "base_shopping_tool", "shopping_")
        ):
            sys.modules.pop(name)
    for name, module in before.items():
        if name.startswith(
            ("native_agent", "tools", "base_shopping_tool", "shopping_")
        ):
            sys.modules[name] = module


def response(content="", calls=(), reasoning=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=content,
                    reasoning_content=reasoning,
                    tool_calls=[
                        SimpleNamespace(
                            id=identifier,
                            function=SimpleNamespace(
                                name=name, arguments=json.dumps(args)
                            ),
                        )
                        for identifier, name, args in calls
                    ],
                )
            )
        ]
    )


def test_native_loop_tools_prompt_and_saved_output(
    runtime, generated, model_config, monkeypatch
):
    root, entry = runtime
    shutil.copy2(model_config, root / "native_agent/models_config.json")
    monkeypatch.setenv("DEEPPLANNING_TEST_KEY", "offline-test-only")
    from native_agent.agent import call_llm

    gold = json.loads(
        (generated / "shopping-level1-case-001/tests/reference.json").read_text()
    )
    scripted = iter(
        [
            response(
                calls=[
                    ("profile", "get_user_info", {}),
                    (
                        "details",
                        "get_product_details",
                        {
                            "product_ids": [
                                gold["ground_truth_products"][0]["product_id"]
                            ]
                        },
                    ),
                ]
            ),
            response("I have a shopping plan.", reasoning="Scripted reasoning"),
            response(
                calls=[
                    ("unknown", "not_a_tool", {}),
                    (
                        "invalid",
                        "add_product_to_cart",
                        {"product_id": "missing", "quantity": 1},
                    ),
                ]
                + [
                    (
                        f"add-{i}",
                        "add_product_to_cart",
                        {"product_id": p["product_id"], "quantity": 1},
                    )
                    for i, p in enumerate(gold["ground_truth_products"])
                ]
                + [("cart", "get_cart_info", {})]
            ),
            response("The requested items are in the cart."),
        ]
    )
    requests = []

    def create(**kwargs):
        requests.append(copy.deepcopy(kwargs))
        return next(scripted)

    monkeypatch.setattr(
        call_llm.openai,
        "OpenAI",
        lambda **kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        ),
    )
    summary = entry.run("offline-model", 4)
    saved = json.loads((root / "state/messages.json").read_text())
    messages = saved["messages"]
    prompt = json.loads((root / "prompt.json").read_text())
    schema = json.loads((root / "tools/shopping_tool_schema.json").read_text())
    assert len(requests) == 4
    assert requests[0]["messages"] == [
        {"role": "system", "content": prompt["system"]},
        {"role": "user", "content": prompt["query"]},
    ]
    assert requests[0]["tools"] == schema
    assert requests[0]["temperature"] == 0.4
    assert requests[0]["extra_body"] == {"max_tokens": 128}
    assert requests[0]["model"] == "offline-model"
    assert (
        "Check whether the items in the shopping cart meet the requirements."
        in requests[2]["messages"][-1]["content"]
    )
    assert messages[5]["reasoning_content"] == "Scripted reasoning"
    errors = [
        json.loads(m["content"])
        for m in messages
        if m["role"] == "tool" and '"error"' in m["content"]
    ]
    assert len(errors) == 2
    assert summary["is_completed"]
    assert messages[-1] == {
        "role": "assistant",
        "content": "The requested items are in the cart.",
    }
    evaluator_spec = importlib.util.spec_from_file_location(
        "upstream_eval",
        generated / "shopping-level1-case-001/tests/evaluation_pipeline.py",
    )
    evaluator = importlib.util.module_from_spec(evaluator_spec)
    evaluator_spec.loader.exec_module(evaluator)
    shutil.copy2(
        generated / "shopping-level1-case-001/tests/reference.json",
        root / "state/validation_cases.json",
    )
    report = evaluator.evaluate_single_case(root / "state")
    assert report["case_score"] == report["score"] == 1.0
    assert report["is_completed"]


def test_per_stage_limit_keeps_upstream_incomplete_state(
    runtime, model_config, monkeypatch
):
    root, entry = runtime
    shutil.copy2(model_config, root / "native_agent/models_config.json")
    monkeypatch.setenv("DEEPPLANNING_TEST_KEY", "offline-test-only")
    from native_agent.agent import call_llm

    requests = []

    def create(**kwargs):
        requests.append(copy.deepcopy(kwargs))
        return response(calls=[(f"call-{len(requests)}", "get_user_info", {})])

    monkeypatch.setattr(
        call_llm.openai,
        "OpenAI",
        lambda **kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        ),
    )
    summary = entry.run("offline-model", 1)
    assert len(requests) == 2  # One call per stage, preserving upstream semantics.
    assert not summary["is_completed"]
    assert (
        json.loads((root / "state/messages.json").read_text())["messages"][-1]["role"]
        == "tool"
    )


def test_checksum_mismatch_fails_before_client_creation(runtime):
    root, entry = runtime
    path = root / "native_agent/agent/shopping_agent.py"
    path.write_text(path.read_text() + "\n# tampered\n")
    with pytest.raises(ValueError, match="checksum mismatch"):
        entry.run("offline-model", 1)


@pytest.mark.parametrize("recover", [False, True])
def test_upstream_retry_policy_and_zero_temperature(
    runtime, model_config, monkeypatch, recover
):
    root, entry = runtime
    data = json.loads(model_config.read_text())
    data["models"]["offline-model"].update(max_retries=2, backoff=0.25, temperature=0)
    (root / "native_agent/models_config.json").write_text(json.dumps(data))
    monkeypatch.setenv("DEEPPLANNING_TEST_KEY", "offline-test-only")
    from native_agent.agent import call_llm

    requests, sleeps = [], []

    def create(**kwargs):
        requests.append(copy.deepcopy(kwargs))
        if not recover or len(requests) == 1:
            raise RuntimeError("scripted provider failure")
        return response("Finished.")

    monkeypatch.setattr(
        call_llm.openai,
        "OpenAI",
        lambda **kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        ),
    )
    monkeypatch.setattr(call_llm.time, "sleep", sleeps.append)
    if recover:
        assert entry.run("offline-model", 1)["is_completed"]
        assert len(requests) == 3
    else:
        with pytest.raises(RuntimeError, match="scripted provider failure"):
            entry.run("offline-model", 1)
        assert len(requests) == 2
        assert not (root / "state/native-summary.json").exists()
        assert (
            len(json.loads((root / "state/messages.json").read_text())["messages"]) == 2
        )
    assert sleeps == [0.25]
    assert all("temperature" not in request for request in requests)


class RecordingEnvironment:
    def __init__(self, fail=False):
        self.calls = []
        self.uploads = {}
        self.downloads = []
        self.fail = fail

    async def exec(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return ExecResult(return_code=9 if self.fail else 0)

    async def upload_file(self, source_path, target_path):
        self.uploads[target_path] = Path(source_path).read_text()

    async def download_file(self, source_path, target_path):
        self.downloads.append(source_path)
        Path(target_path).write_text("{}")


def test_harbor_lifecycle_keeps_credentials_out_of_files_and_commands(
    tmp_path, model_config, monkeypatch
):
    secret = "offline-sentinel-credential"
    monkeypatch.setenv("DEEPPLANNING_TEST_KEY", secret)
    agent = ShoppingFnAgent(
        logs_dir=tmp_path / "logs",
        model_name="offline-model",
        model_config_path=str(model_config),
        max_llm_calls=2,
    )
    environment = RecordingEnvironment()
    context = AgentContext()
    asyncio.run(agent.setup(environment))
    asyncio.run(
        agent.run(
            "Terminal instructions must not enter the native prompt",
            environment,
            context,
        )
    )
    command, options = environment.calls[-1]
    assert "--max-llm-calls 2" in command
    assert options["env"]["DEEPPLANNING_TEST_KEY"] == secret
    assert secret not in command
    assert all(secret not in value for value in environment.uploads.values())
    assert all(secret not in p.read_text() for p in agent.logs_dir.iterdir())
    assert "/workspace/state/messages.json" in environment.downloads
    assert context.metadata["max_llm_calls_per_stage"] == 2


def test_native_failures_propagate_and_partial_logs_are_collected(
    tmp_path, model_config, monkeypatch
):
    monkeypatch.setenv("DEEPPLANNING_TEST_KEY", "offline-test-only")
    agent = ShoppingFnAgent(
        logs_dir=tmp_path / "logs",
        model_name="offline-model",
        model_config_path=str(model_config),
    )
    environment = RecordingEnvironment(fail=True)
    with pytest.raises(RuntimeError, match="exited 9"):
        asyncio.run(agent.run("", environment, AgentContext()))
    assert len(environment.downloads) == 3


def test_missing_credentials_fail_before_execution(tmp_path, model_config, monkeypatch):
    monkeypatch.delenv("DEEPPLANNING_TEST_KEY", raising=False)
    agent = ShoppingFnAgent(
        logs_dir=tmp_path / "logs",
        model_name="offline-model",
        model_config_path=str(model_config),
    )
    environment = RecordingEnvironment()
    with pytest.raises(ValueError, match="Missing native credential"):
        asyncio.run(agent.run("", environment, AgentContext()))
    assert not environment.calls and not environment.uploads


def test_rejects_embedded_credentials_and_model_aliases(model_config):
    data = json.loads(model_config.read_text())
    data["models"]["offline-model"]["api_key"] = "do-not-persist"
    model_config.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="Unsupported native model"):
        load_model_config(model_config, "offline-model")
    del data["models"]["offline-model"]["api_key"]
    data["models"]["offline-model"]["model_name"] = "different-model"
    model_config.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="exact API model ID"):
        load_model_config(model_config, "offline-model")
