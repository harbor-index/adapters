"""Check generation fidelity, hidden answers, safe writes and the tool transport."""

import importlib.util
import json
import subprocess
import sys

import pytest
from harbor.models.task.task import Task
from harbor.models.task.verifier_mode import resolve_verifier_environment_definition

from deepplanning_shopping.adapter import DOMAIN, TASKS, DeepPlanningShoppingAdapter


def test_every_task_loads_with_isolated_verification(generated):
    for path in generated.iterdir():
        task = Task(path)
        assert task.config.task.name == f"qwen/deepplanning-{path.name}"
        assert task.config.verifier.environment_mode.value == "separate"
        definition = resolve_verifier_environment_definition(task.config, task.paths)
        assert definition.config.docker_image == task.config.environment.docker_image
        assert (
            not definition.bundled_tests
        )  # Harbor must upload tests to the separate VM.


def test_no_reference_or_grader_enters_agent_context(generated):
    for task in generated.iterdir():
        environment = task / "environment"
        assert not list(environment.rglob("reference.json"))
        assert not list(environment.rglob("validation_cases.json"))
        assert not list(environment.rglob("evaluation_pipeline.py"))
        assert not (environment / "tests").exists()
        assert not (environment / "solution").exists()
        assert (task / "tests/reference.json").read_bytes() == (
            task / "solution/reference.json"
        ).read_bytes()


def test_upstream_tools_and_grader_are_byte_identical(generated, inputs):
    domain = inputs["source_dir"] / DOMAIN
    for task in generated.iterdir():
        assert (task / "tests/evaluation_pipeline.py").read_bytes() == (
            domain / "evaluation/evaluation_pipeline.py"
        ).read_bytes()
        for source in (domain / "tools").iterdir():
            if source.is_file() and source.suffix in (".py", ".json"):
                assert (
                    task / "environment/tools" / source.name
                ).read_bytes() == source.read_bytes()
        for name in ("shopping_agent.py", "call_llm.py"):
            assert (task / "environment/native_agent/agent" / name).read_bytes() == (
                domain / "agent" / name
            ).read_bytes()


def test_prompts_match_released_dataset_and_correction_is_explicit(generated):
    corrections = []
    for path in generated.iterdir():
        gold = json.loads((path / "tests/reference.json").read_text())
        prompt = json.loads((path / "environment/prompt.json").read_text())
        assert prompt["query"] == gold["query"]
        instruction = (path / "instruction.md").read_text()
        assert prompt["system"] in instruction and prompt["query"] in instruction
        if (
            json.loads((path / "provenance.json").read_text())["query_source"]
            == "dataset-v1.1"
        ):
            corrections.append(path.name)
    assert corrections == ["shopping-level1-case-036"]


def test_task_selection_and_parity_are_stable(tmp_path):
    parity = DeepPlanningShoppingAdapter(tmp_path / "parity", split="parity")
    assert len(parity.task_ids) == 10
    assert sum(TASKS[t][0] == 1 for t in parity.task_ids) == 5
    selected = [list(TASKS)[50], next(iter(TASKS))]
    adapter = DeepPlanningShoppingAdapter(
        tmp_path / "selected", task_ids=selected, limit=1
    )
    assert adapter.task_ids == [next(iter(TASKS))]


@pytest.mark.parametrize(
    "kwargs",
    [{"limit": 0}, {"limit": -1}, {"task_ids": []}, {"task_ids": ["../escape"]}],
)
def test_invalid_selections_fail_without_download(tmp_path, kwargs):
    with pytest.raises(ValueError):
        DeepPlanningShoppingAdapter(tmp_path / "out", **kwargs)


def test_existing_output_requires_opt_in_and_owned_marker(tmp_path, inputs):
    output = tmp_path / "out"
    DeepPlanningShoppingAdapter(output, limit=1, **inputs).run()
    sentinel = output / next(iter(TASKS)) / "keep"
    sentinel.write_text("keep")
    with pytest.raises(FileExistsError):
        DeepPlanningShoppingAdapter(output, limit=1, **inputs).run()
    provenance = sentinel.parent / "provenance.json"
    provenance.write_text('{"adapter":"unrelated"}')
    with pytest.raises(ValueError, match="unrelated"):
        DeepPlanningShoppingAdapter(output, limit=1, overwrite=True, **inputs).run()
    assert sentinel.read_text() == "keep"


def test_docker_variant_has_separate_build_contexts(tmp_path, inputs):
    path = DeepPlanningShoppingAdapter(
        tmp_path / "docker", environment="docker", limit=1, **inputs
    ).run()[0]
    task = Task(path)
    assert (path / "environment/Dockerfile").is_file()
    assert (path / "tests/Dockerfile").is_file()
    assert not task.config.environment.docker_image
    definition = resolve_verifier_environment_definition(task.config, task.paths)
    assert definition.bundled_tests and definition.directory == path / "tests"


def test_cli_has_required_flags():
    result = subprocess.run(
        [sys.executable, "-m", "deepplanning_shopping.main", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    for flag in ("--output-dir", "--limit", "--overwrite", "--task-ids"):
        assert flag in result.stdout


def test_tool_transport_replays_gold_and_records_real_tool_calls(tmp_path, generated):
    source = generated / "shopping-level1-case-001/environment"
    import shutil

    runtime = tmp_path / "runtime"
    shutil.copytree(source, runtime)
    spec = importlib.util.spec_from_file_location(
        "shopping_runtime", runtime / "shop.py"
    )
    shop = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shop)
    gold = json.loads(
        (generated / "shopping-level1-case-001/tests/reference.json").read_text()
    )
    for product in gold["ground_truth_products"]:
        result = shop.call(
            "add_product_to_cart", {"product_id": product["product_id"], "quantity": 1}
        )
        assert "error" not in result
    result = shop.call("get_cart_info", {})
    assert "error" not in result
    shop.finish("Finished selecting the requested items.")
    messages = json.loads((runtime / "state/messages.json").read_text())["messages"]
    assert messages[-1]["role"] == "assistant" and not messages[-1].get("tool_calls")
    assert any(m["role"] == "tool" for m in messages)
    cart = json.loads((runtime / "state/cart.json").read_text())
    assert {p["product_id"] for p in cart["items"]} == {
        p["product_id"] for p in gold["ground_truth_products"]
    }
    assert "error" in shop.call(
        "add_product_to_cart", {"product_id": "missing", "quantity": 1}
    )
