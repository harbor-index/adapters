"""Trusted TabArena scoring with a separately privileged, frozen Python predictor.

Submitted Python runs only as UID/GID 10001, never in the scoring process. Harbor
must invoke this verifier as root in a fresh, no-network verifier environment.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import resource
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

import numpy as np
import pandas as pd
from autogluon.core.data.label_cleaner import LabelCleaner
from autogluon.core.metrics import get_metric

PREDICT_UID = 10001
PREDICT_GID = 10001
MAX_BUNDLE_BYTES = 256 * 1024 * 1024
MAX_OUTPUT_BYTES = 256 * 1024 * 1024
MAX_FILES = 4096
WALL_SECONDS = 180
CPU_SECONDS = 160
MEMORY_BYTES = 4 * 1024**3


class InvalidSubmission(ValueError):
    """The ordinary predictor/output contract was not satisfied."""


def _json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidSubmission(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _json_constant(value):
    raise InvalidSubmission(f"Nonfinite JSON constant: {value}")


def read_json(path, limit):
    """Read a bounded regular file without following links or importing code."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise InvalidSubmission(
            f"Missing or unsafe output file: {exc.strerror}"
        ) from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise InvalidSubmission("Output must be a regular file with one link")
        if info.st_size > limit:
            raise InvalidSubmission("Output exceeds the declared size limit")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            content = stream.read(limit + 1)
        if len(content) > limit:
            raise InvalidSubmission("Output exceeds the declared size limit")
        return json.loads(
            content, object_pairs_hook=_json_pairs, parse_constant=_json_constant
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise InvalidSubmission("Malformed prediction JSON") from exc
    finally:
        os.close(descriptor)


def freeze_submission(source, destination):
    """Copy a static submission into a root-owned, child-read-only bundle."""
    source, destination = Path(source), Path(destination)
    if source.is_symlink() or not source.is_dir():
        raise InvalidSubmission("Missing or unsafe submission directory")
    destination.mkdir(mode=0o755)
    count = total = 0
    hashes = {}
    for directory, names, files in os.walk(source, followlinks=False):
        names.sort()
        files.sort()
        relative = Path(directory).relative_to(source)
        target_dir = destination / relative
        if relative.parts:
            target_dir.mkdir(mode=0o755, exist_ok=True)
        for name in names:
            count += 1
            if count > MAX_FILES:
                raise InvalidSubmission("Too many submission entries")
            entry = Path(directory) / name
            info = entry.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise InvalidSubmission("Bundle directories cannot be links or devices")
        for name in files:
            count += 1
            if count > MAX_FILES:
                raise InvalidSubmission("Too many submission files")
            entry = Path(directory) / name
            try:
                descriptor = os.open(entry, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except OSError as exc:
                raise InvalidSubmission("Unsafe submission entry") from exc
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise InvalidSubmission(
                        "Submission files must be regular and unlinked"
                    )
                total += info.st_size
                if total > MAX_BUNDLE_BYTES:
                    raise InvalidSubmission("Submission exceeds 256 MiB")
                target = target_dir / name
                digest = hashlib.sha256()
                with os.fdopen(descriptor, "rb", closefd=False) as incoming:
                    with target.open("wb") as outgoing:
                        while block := incoming.read(1024 * 1024):
                            digest.update(block)
                            outgoing.write(block)
                target.chmod(0o444)
                hashes[str(target.relative_to(destination))] = digest.hexdigest()
            finally:
                os.close(descriptor)
    if not (destination / "predict.py").is_file():
        raise InvalidSubmission("Missing predict.py")
    for directory, _, _ in os.walk(destination, topdown=False):
        Path(directory).chmod(0o555)
    return hashes


def native_permutation(size):
    """Pinned native exec_models/utils.py::_make_perm, seed=0."""
    permutation = np.random.default_rng(0).permutation(size)
    inverse = np.empty_like(permutation)
    inverse[permutation] = np.arange(size)
    return permutation, inverse


def validate_predictions(value, metadata, rows):
    if not isinstance(value, dict) or set(value) != {"classes", "predictions"}:
        raise InvalidSubmission("Output needs exactly classes and predictions")
    if value["classes"] != metadata["classes"]:
        raise InvalidSubmission("Class order mismatch")
    width = 1 if metadata["problem_type"] == "regression" else len(metadata["classes"])
    values = value["predictions"]
    if not isinstance(values, list) or len(values) != rows:
        raise InvalidSubmission("Prediction row count mismatch")
    if any(
        not isinstance(row, list)
        or len(row) != width
        or any(isinstance(x, bool) or not isinstance(x, (int, float)) for x in row)
        for row in values
    ):
        raise InvalidSubmission("Predictions must be an N-by-K numeric matrix")
    try:
        predictions = np.asarray(values, dtype=np.float64)
    except (OverflowError, ValueError) as exc:
        raise InvalidSubmission("Predictions cannot be represented as float64") from exc
    if predictions.shape != (rows, width) or not np.isfinite(predictions).all():
        raise InvalidSubmission("Invalid shape or nonfinite predictions")
    if metadata["problem_type"] != "regression":
        if (predictions < 0).any() or (predictions > 1).any():
            raise InvalidSubmission("Probabilities must be within [0,1]")
        # This is only a floating-point representation check, not a score threshold.
        if not np.allclose(predictions.sum(axis=1), 1.0, atol=1e-12, rtol=0):
            raise InvalidSubmission("Probability rows must sum to one")
    return predictions


def native_score(predictions, labels, metadata, training_labels):
    """The exact native evaluate path: LabelCleaner then AutoGluon scorer.error."""
    problem = metadata["problem_type"]
    scorer = get_metric(metadata["metric"], problem_type=problem)
    cleaner = LabelCleaner.construct(problem_type=problem, y=pd.Series(training_labels))
    if (
        problem != "regression"
        and list(cleaner.ordered_class_labels) != metadata["classes"]
    ):
        raise RuntimeError(
            "Trusted class order differs from native training LabelCleaner"
        )
    y_true = cleaner.transform(pd.Series(labels))
    if problem == "regression":
        y_pred = cleaner.transform(pd.Series(predictions[:, 0]))
        error = scorer.error(y_true=y_true, y_pred=y_pred)
        gold = scorer.error(y_true=y_true, y_pred=y_true.copy())
    else:
        columns = metadata["classes"]
        probabilities = cleaner.transform_proba(
            pd.DataFrame(predictions, columns=columns), as_pandas=True
        )
        ideal = pd.DataFrame(
            [[float(label == column) for column in columns] for label in labels],
            columns=columns,
            dtype=np.float64,
        )
        ideal = cleaner.transform_proba(ideal, as_pandas=True)
        if problem == "binary":
            probabilities = pd.DataFrame(probabilities).iloc[:, 1]
            ideal = pd.DataFrame(ideal).iloc[:, 1]
        error = scorer.error(y_true=y_true, y_pred=probabilities)
        gold = scorer.error(y_true=y_true, y_pred=ideal)
    error, gold = float(error), float(gold)
    if not np.isfinite(error) or not np.isfinite(gold):
        raise InvalidSubmission("Native scorer produced a nonfinite result")
    return {
        "native_error": error,
        "native_optimum_error": gold,
        "metric_optimal": int(error == gold),
        "native_metric": metadata["metric"],
        "metric_direction": "lower_is_better",
        "probability_dtype": "float64",
    }


def read_frame(path, metadata):
    """Preserve JSON null versus nominal strings such as NA/nan/empty string."""
    value = json.loads(Path(path).read_text())
    if set(value) != {"columns", "data"}:
        raise RuntimeError("Trusted frame requires columns/data schema")
    frame = pd.DataFrame(value["data"], columns=value["columns"])
    for feature in metadata["features"]:
        if feature["type"] == "numeric":
            frame[feature["name"]] = pd.to_numeric(
                frame[feature["name"]], errors="raise"
            )
    return frame


def _kill_predictor_processes():
    """Dedicated UID is confined to this verifier; catch detached descendants too."""
    for attempt in range(20):
        running = []
        for directory in Path("/proc").iterdir():
            if not directory.name.isdigit():
                continue
            try:
                lines = (directory / "status").read_text().splitlines()
                fields = {
                    line.split(":", 1)[0]: line.split(":", 1)[1].strip()
                    for line in lines
                    if ":" in line
                }
                if int(fields["Uid"].split()[0]) == PREDICT_UID and not fields[
                    "State"
                ].startswith("Z"):
                    running.append(int(directory.name))
            except (
                FileNotFoundError,
                ProcessLookupError,
                PermissionError,
                KeyError,
                ValueError,
            ):
                continue
        if not running:
            return
        for pid in running:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        time.sleep(0.05)
    raise InvalidSubmission("Cannot terminate all predictor descendants")


def _child_limits():
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS))
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT_BYTES, MAX_OUTPUT_BYTES))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        raise OSError(ctypes.get_errno(), "Cannot prohibit child privilege gain")


def run_predictor(bundle, query, scratch):
    output = scratch / "predictions.json"
    environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(scratch),
        "TMPDIR": str(scratch),
        "LANG": "C.UTF-8",
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    log_path = scratch.parent / "predictor.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-E",
                "-s",
                str(bundle / "predict.py"),
                str(query),
                str(output),
            ],
            cwd=bundle,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            user=PREDICT_UID,
            group=PREDICT_GID,
            extra_groups=(),
            start_new_session=True,
            preexec_fn=_child_limits,
        )
        try:
            process.wait(timeout=WALL_SECONDS)
        except subprocess.TimeoutExpired as exc:
            raise InvalidSubmission("Predictor exceeded 180-second wall limit") from exc
        finally:
            # Also terminate descendants of a predictor that exits before its workers.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
            _kill_predictor_processes()
    if process.returncode:
        with log_path.open("rb") as log:
            log.seek(max(0, log_path.stat().st_size - 2000))
            tail = log.read().decode(errors="replace")
        raise InvalidSubmission(f"Predictor exited {process.returncode}: {tail}")
    return output


def evaluate(submission, tests, output, training):
    tests, output = Path(tests).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    result = {"status": "invalid_submission", "metric_optimal": 0}
    if os.geteuid() != 0 or sys.platform != "linux":
        raise RuntimeError(
            "Protected predictor evaluation requires Linux root verifier"
        )
    # These privileged directories are never traversable by the predictor UID.
    tests.chmod(0o700)
    output.chmod(0o700)
    for name in ("reward.txt", "reward.json", "result.json", "predictions.json"):
        (output / name).unlink(missing_ok=True)
    try:
        metadata = json.loads((tests / "metadata.json").read_text())
        labels = json.loads((tests / "test_labels.json").read_text())
        raw_test = json.loads((tests / "test.json").read_text())
        frame = read_frame(tests / "test.json", metadata)
        training_labels = read_frame(training, metadata)[metadata["target"]]
        if len(frame) != len(labels):
            raise RuntimeError("Trusted test features/labels are misaligned")
        permutation, inverse = native_permutation(len(frame))
        with tempfile.TemporaryDirectory(prefix="tabarena-predict-") as temporary:
            base = Path(temporary)
            base.chmod(0o711)
            bundle, query, scratch = (
                base / name for name in ("bundle", "query", "scratch")
            )
            hashes = freeze_submission(submission, bundle)
            query.mkdir(mode=0o755)
            shuffled_frame = frame.iloc[permutation].reset_index(drop=True)
            shuffled_frame.to_csv(query / "test.csv", index=False)
            (query / "test.json").write_text(
                json.dumps(
                    {
                        "columns": raw_test["columns"],
                        "data": [raw_test["data"][int(index)] for index in permutation],
                    },
                    allow_nan=False,
                )
            )
            (query / "metadata.json").write_text(json.dumps(metadata))
            for path in query.iterdir():
                path.chmod(0o444)
            query.chmod(0o555)
            scratch.mkdir(mode=0o700)
            os.chown(scratch, PREDICT_UID, PREDICT_GID)
            predicted = run_predictor(bundle, query, scratch)
            width = (
                1
                if metadata["problem_type"] == "regression"
                else len(metadata["classes"])
            )
            size_limit = min(MAX_OUTPUT_BYTES, 1024 * 1024 + len(frame) * width * 32)
            value = read_json(predicted, size_limit)
            shuffled = validate_predictions(value, metadata, len(frame))
            predictions = shuffled[inverse]
            result = {
                "status": "scored",
                "runtime": {
                    name: importlib.metadata.version(name)
                    for name in (
                        "numpy",
                        "pandas",
                        "scikit-learn",
                        "autogluon.core",
                        "autogluon.common",
                        "autogluon.features",
                    )
                },
                "verifier_sha256": hashlib.sha256(
                    Path(__file__).read_bytes()
                ).hexdigest(),
                **native_score(predictions, labels, metadata, training_labels),
                "n_test": len(frame),
                "classes": metadata["classes"],
                "submission_sha256": hashes,
                "prediction_boundary": "Linux low-privilege Python child; no-network Harbor verifier",
                "test_permutation": "numpy.default_rng(0).permutation(n); inverse restored",
            }
            (output / "predictions.json").write_text(
                json.dumps(predictions.tolist(), allow_nan=False)
            )
            shutil.copyfile(base / "predictor.log", output / "predictor.log")
    except (
        InvalidSubmission,
        OSError,
        ValueError,
        OverflowError,
        RecursionError,
        subprocess.SubprocessError,
    ) as exc:
        result = {
            "status": "invalid_submission",
            "metric_optimal": 0,
            "error": str(exc),
        }
    rewards = {"metric_optimal": result["metric_optimal"]}
    if result["status"] == "scored":
        rewards["native_error"] = result["native_error"]
    (output / "reward.json").write_text(json.dumps(rewards, allow_nan=False))
    (output / "result.json").write_text(json.dumps(result, indent=2, allow_nan=False))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--submission", type=Path, default=Path("/workspace/submission")
    )
    parser.add_argument("--tests", type=Path, default=Path("/tests"))
    parser.add_argument("--output", type=Path, default=Path("/logs/verifier"))
    parser.add_argument(
        "--training", type=Path, default=Path("/workspace/data/train.json")
    )
    args = parser.parse_args()
    evaluate(args.submission, args.tests, args.output, args.training)
