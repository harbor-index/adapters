"""Executable scorer/output/bundle controls for the TabArena-Lite interface.

Linux privilege isolation requires separate actual Modal probes; these unit tests
are deliberately not described as evidence that macOS provided that isolation.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import numpy as np
import pytest

TEMPLATE = Path(__file__).parents[1] / "src/tabarena/task-template"
spec = importlib.util.spec_from_file_location(
    "lite_trusted_verifier", TEMPLATE / "tests/verifier.py"
)
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


def metadata(problem="binary"):
    return {
        "features": [
            {"name": "number", "type": "numeric"},
            {"name": "category", "type": "categorical"},
        ],
        "target": "target",
        "classes": []
        if problem == "regression"
        else (["No", "Yes"] if problem == "binary" else ["a", "b", "c"]),
        "problem_type": problem,
        "metric": {"binary": "roc_auc", "multiclass": "log_loss", "regression": "rmse"}[
            problem
        ],
    }


def output(predictions, classes):
    return {"classes": classes, "predictions": predictions}


@pytest.mark.parametrize("problem", ["binary", "multiclass", "regression"])
def test_native_exact_answer_and_wrong_predictions(problem):
    meta = metadata(problem)
    labels = (
        [0.125, -2.5, 3.0, 0.125]
        if problem == "regression"
        else (
            ["No", "Yes", "No", "Yes"] if problem == "binary" else ["a", "b", "c", "a"]
        )
    )
    predictions = (
        np.array(labels, dtype=float).reshape(-1, 1)
        if problem == "regression"
        else np.array([[float(y == c) for c in meta["classes"]] for y in labels])
    )
    gold = verifier.native_score(predictions, labels, meta, labels)
    assert gold["metric_optimal"] == 1
    assert gold["native_error"] == gold["native_optimum_error"]
    if problem == "multiclass":
        assert gold["native_error"] == pytest.approx(
            2.2204460492503136e-16, rel=0, abs=0
        )
        wrong = np.full_like(predictions, 1 / 3)
    elif problem == "binary":
        assert gold["native_error"] == 0
        wrong = np.full_like(predictions, 0.5)
    else:
        assert gold["native_error"] == 0
        wrong = predictions + 2
    scored = verifier.native_score(wrong, labels, meta, labels)
    assert scored["metric_optimal"] == 0
    assert scored["native_error"] > gold["native_error"]


def test_binary_reversed_probability_ranking():
    meta = metadata()
    scored = verifier.native_score(
        np.array([[0, 1], [1, 0]]), ["No", "Yes"], meta, ["No", "Yes"]
    )
    assert scored["native_error"] == 1
    assert scored["metric_optimal"] == 0


def test_numeric_class_label_cleaner_order():
    meta = metadata()
    meta["classes"] = [1, 2]
    result = verifier.native_score(np.array([[0, 1], [1, 0]]), [2, 1], meta, [2, 1, 2])
    assert result["native_error"] == 0
    assert result["metric_optimal"] == 1


@pytest.mark.parametrize(
    "problem,predictions",
    [
        ("binary", [[0.25, 0.75], [1, 0]]),
        ("multiclass", [[0.25, 0.25, 0.5], [1, 0, 0]]),
        ("regression", [[1.25], [-2.0]]),
    ],
)
def test_valid_shapes(problem, predictions):
    meta = metadata(problem)
    parsed = verifier.validate_predictions(
        output(predictions, meta["classes"]), meta, 2
    )
    assert parsed.dtype == np.float64
    assert np.array_equal(parsed, predictions)


@pytest.mark.parametrize(
    "predictions",
    [
        [],
        [[1, 0]],
        [[1, 0], [0, 1], [1, 0]],
        [1, 0],
        [[1], [0]],
        [[1, 0, 0], [0, 1, 0]],
        [[True, False], [False, True]],
        [["1", 0], [0, 1]],
        [[float("nan"), 0], [0, 1]],
        [[float("inf"), 0], [0, 1]],
        [[-0.1, 1.1], [0, 1]],
        [[0.25, 0.25], [0, 1]],
    ],
)
def test_invalid_predictions(predictions):
    meta = metadata()
    with pytest.raises(verifier.InvalidSubmission):
        verifier.validate_predictions(output(predictions, meta["classes"]), meta, 2)


@pytest.mark.parametrize(
    "bad",
    [
        {"classes": ["Yes", "No"], "predictions": [[1, 0], [0, 1]]},
        {
            "classes": ["No", "Yes"],
            "predictions": [[1, 0], [0, 1]],
            "metric_optimal": 1,
        },
        {"predictions": [[1, 0], [0, 1]]},
        [1, 2],
        None,
    ],
)
def test_class_and_schema_validation(bad):
    with pytest.raises(verifier.InvalidSubmission):
        verifier.validate_predictions(bad, metadata(), 2)


def test_native_permutation_and_duplicate_rows():
    permutation, inverse = verifier.native_permutation(9)
    assert np.array_equal(permutation, np.random.default_rng(0).permutation(9))
    values = np.arange(9)
    assert np.array_equal(values[permutation][inverse], values)
    # Identical feature rows do not force a known output oracle to use one value.
    labels = ["No", "Yes", "No", "Yes"]
    predictions = np.array([[1, 0], [0, 1], [1, 0], [0, 1]])
    assert (
        verifier.native_score(predictions, labels, metadata(), labels)["metric_optimal"]
        == 1
    )


def test_json_preserves_null_and_literal_nominal_strings(tmp_path):
    path = tmp_path / "frame.json"
    frame = {
        "columns": ["number", "category"],
        "data": [[1.0000000000000002, "NA"], [None, "nan"], [2, ""], [3, None]],
    }
    path.write_text(json.dumps(frame))
    parsed = verifier.read_frame(path, metadata())
    assert parsed.iloc[0, 0] == 1.0000000000000002
    assert parsed["category"].tolist() == ["NA", "nan", "", None]
    assert np.isnan(parsed.iloc[1, 0])


@pytest.mark.parametrize(
    "contents", ["not JSON", '{"predictions": NaN}', '{"a":1,"a":2}']
)
def test_bad_json_file(tmp_path, contents):
    path = tmp_path / "out.json"
    path.write_text(contents)
    with pytest.raises(verifier.InvalidSubmission):
        verifier.read_json(path, 1024)


def test_json_file_missing_symlink_hardlink_oversized(tmp_path):
    source = tmp_path / "source"
    source.write_text("{}")
    link = tmp_path / "link"
    link.symlink_to(source)
    with pytest.raises(verifier.InvalidSubmission):
        verifier.read_json(link, 1024)
    with pytest.raises(verifier.InvalidSubmission):
        verifier.read_json(tmp_path / "missing", 1024)
    with pytest.raises(verifier.InvalidSubmission):
        verifier.read_json(source, 1)
    hard = tmp_path / "hard"
    os.link(source, hard)
    with pytest.raises(verifier.InvalidSubmission):
        verifier.read_json(hard, 1024)


def test_regular_json_read(tmp_path):
    path = tmp_path / "out.json"
    path.write_text('{"answer":1}')
    assert verifier.read_json(path, 1024) == {"answer": 1}


def test_freeze_preserves_bundle_and_does_not_execute_python(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    marker = tmp_path / "executed"
    (source / "predict.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
    )
    (source / "model.bin").write_bytes(b"serialized model")
    destination = tmp_path / "bundle"
    hashes = verifier.freeze_submission(source, destination)
    assert set(hashes) == {"predict.py", "model.bin"}
    assert not marker.exists()
    assert (destination / "predict.py").stat().st_mode & 0o222 == 0
    assert destination.stat().st_mode & 0o222 == 0


@pytest.mark.parametrize(
    "entry_kind",
    ["symlink", "directory_symlink", "hardlink", "fifo", "missing_predictor"],
)
def test_freeze_rejects_unsafe_entries(tmp_path, entry_kind):
    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "safe"
    target.write_text("data")
    if entry_kind != "missing_predictor":
        (source / "predict.py").write_text("pass")
    if entry_kind == "symlink":
        (source / "bad").symlink_to(target)
    if entry_kind == "directory_symlink":
        (source / "bad").symlink_to(tmp_path, target_is_directory=True)
    if entry_kind == "hardlink":
        os.link(target, source / "bad")
    if entry_kind == "fifo":
        os.mkfifo(source / "bad")
    with pytest.raises(verifier.InvalidSubmission):
        verifier.freeze_submission(source, tmp_path / "bundle")


def test_bundle_resource_bounds(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "predict.py").write_text("12345")
    monkeypatch.setattr(verifier, "MAX_BUNDLE_BYTES", 4)
    with pytest.raises(verifier.InvalidSubmission):
        verifier.freeze_submission(source, tmp_path / "bundle")


def test_requires_real_linux_privilege_boundary(tmp_path):
    if os.geteuid() == 0 and os.sys.platform == "linux":
        pytest.skip("Host is suitable for integration, not this refusal control")
    with pytest.raises(RuntimeError, match="Linux root"):
        verifier.evaluate(
            tmp_path, tmp_path, tmp_path / "logs", tmp_path / "train.json"
        )


# Parent-owned cloud integration can use this literal as a submitted predict.py.
# Successful execution proves each assertion in the actual low-privilege child;
# this fixture is not executed on the local macOS test runner.
MALICIOUS_PREDICTOR = r"""
import json, os, socket, sys
from pathlib import Path
query, output = map(Path, sys.argv[1:])
checks = {"uid": os.getuid(), "gid": os.getgid(), "groups": os.getgroups()}
assert os.getuid() == 10001 and os.getgid() == 10001 and not os.getgroups()
status = Path('/proc/self/status').read_text()
assert 'NoNewPrivs:\t1' in status
for path in ['/tests/test_labels.json', '/tests/verifier.py', '/tests/metadata.json', f'/proc/{os.getppid()}/environ']:
    try:
        Path(path).read_bytes()
    except PermissionError:
        checks[path] = 'denied'
    else:
        raise AssertionError('Protected read succeeded: ' + path)
for path in ['/logs/verifier/reward.json', str(Path.cwd()/'predict.py'), str(query/'test.json')]:
    try:
        Path(path).write_text('tamper')
    except PermissionError:
        checks[path] = 'denied'
    else:
        raise AssertionError('Protected write succeeded: ' + path)
try:
    os.setuid(0)
except PermissionError:
    checks['setuid0'] = 'denied'
else:
    raise AssertionError('Privilege escalation succeeded')
assert not any('KEY' in key or 'TOKEN' in key or 'SECRET' in key for key in os.environ)
try:
    socket.create_connection(('www.openml.org',443),timeout=3).close()
except OSError:
    checks['network'] = 'denied'
else:
    raise AssertionError('Unexpected network access')
metadata = json.loads((query/'metadata.json').read_text())
rows = json.loads((query/'test.json').read_text())['data']
classes = metadata['classes']
value = [0.0] if not classes else [1/len(classes)]*len(classes)
output.write_text(json.dumps({'classes':classes,'predictions':[value for _ in rows]}))
print(json.dumps(checks,sort_keys=True))
"""


def test_huge_integer_is_explicit_invalid_submission():
    with pytest.raises(verifier.InvalidSubmission, match="float64"):
        verifier.validate_predictions(
            output([[10**400]], []), metadata("regression"), 1
        )


def test_deep_json_is_explicit_invalid_submission(tmp_path):
    path = tmp_path / "deep.json"
    path.write_text("[" * 2000 + "0" + "]" * 2000)
    with pytest.raises(verifier.InvalidSubmission):
        verifier.validate_predictions(verifier.read_json(path, 10000), metadata(), 1)
