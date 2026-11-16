"""Validate the trusted known-answer oracle through pinned native TabArena.

This executes only the generated, hash-checked solution/predict.py. It is not
an isolation runner for submitted agent code and performs no remote/model calls.
Set PYTHONPATH to the pinned upstream packages, not this adapter package.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import inspect
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import numpy as np
import pandas as pd
from autogluon.core.data.label_cleaner import LabelCleaner
from autogluon.core.metrics import get_metric
from tabarena.benchmark.exec_models.external import ExternalSystemModel
from tabarena.benchmark.experiment.experiment_runner import evaluate

PIN = "bfa8c50c17885e5b722d75ce5bcb5db3822257a3"
NATIVE_SOURCE_HASHES = {
    "benchmark/exec_models/external.py": "086e2bcf7cc413e9f540de1c1b4ff7b584bd1fb3099173fabdf7c205f9a0f919",
    "benchmark/exec_models/base.py": "5d03686981ee7bd9f2def508e1fb6b10c055b057d9be1a722a56b6ab01c01e7c",
    "benchmark/exec_models/utils.py": "3d6daf88a87f96ea975cb896c562362fdda94618f2a0039eaadf37507acc92f7",
    "benchmark/experiment/experiment_runner.py": "4f049ce932460aefdb17205b6d2209b8555320df548820dd2c550c7c21950351",
}
TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "src/tabarena/task-template/solution/predict.py"
)


def save(path, value):
    Path(path).write_text(
        json.dumps(value, indent=2, allow_nan=False, default=str) + "\n"
    )


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def frame_from_json(path, metadata):
    """Use typed JSON; literal NA/nan/empty strings remain nominal values."""
    value = json.loads(Path(path).read_text())
    if set(value) != {"columns", "data"}:
        raise ValueError("Trusted frame must contain columns and data")
    frame = pd.DataFrame(value["data"], columns=value["columns"])
    for feature in metadata["features"]:
        name = feature["name"]
        if feature["type"] == "numeric":
            frame[name] = pd.to_numeric(frame[name], errors="raise")
        elif "categories" in feature:
            frame[name] = pd.Categorical(frame[name], categories=feature["categories"])
    return frame


class KnownAnswerOracle(ExternalSystemModel):
    """Privileged reference outputs through the ordinary external-system API.

    The reference outputs are supplied deliberately as oracle assets. They are
    not learned from training features and cannot be counted as model quality.
    Native fit_custom owns test shuffling/reset and output-order restoration.
    """

    def __init__(self, *, task_dir, output, metadata, training_X, training_y, **kwargs):
        super().__init__(**kwargs)
        self.task_dir = Path(task_dir)
        self.output = Path(output)
        self.metadata = metadata
        self.training_X = training_X
        self.training_y = training_y
        self.bundle = self.output / "oracle_submission"
        self.predict_calls = 0

    def _fit_system(
        self,
        X,
        y,
        *,
        target_name,
        problem_type,
        eval_metric,
        validation_metadata,
        num_cpus,
        num_gpus,
        memory_limit,
        time_limit,
        random_state=None,
    ):
        pd.testing.assert_frame_equal(X, self.training_X)
        pd.testing.assert_series_equal(y, self.training_y)
        if (
            target_name != self.metadata["target"]
            or problem_type != self.metadata["problem_type"]
        ):
            raise ValueError("Native fit contract disagrees with task metadata")
        self.bundle.mkdir()
        source = self.task_dir / "solution/predict.py"
        if digest(source) != digest(TEMPLATE):
            raise ValueError(
                "Refusing to execute a predictor other than the trusted oracle template"
            )
        for name in ("predict.py", "answers.json"):
            shutil.copyfile(self.task_dir / "solution" / name, self.bundle / name)
        save(
            self.output / "fit_contract.json",
            {
                "role": "known-answer oracle, not trained baseline or independent agent",
                "fit_rows": len(X),
                "fit_columns": list(X.columns),
                "test_rows_or_labels_passed_to_fit": False,
                "privileged_reference_answers": "solution/answers.json copied into oracle bundle",
                "random_state": random_state,
                "budget": {
                    "num_cpus": num_cpus,
                    "num_gpus": num_gpus,
                    "memory_limit_gb": memory_limit,
                    "time_limit_seconds": time_limit,
                },
                "predict_sha256": digest(self.bundle / "predict.py"),
                "answers_sha256": digest(self.bundle / "answers.json"),
            },
        )
        return self

    def _respond(self, X):
        self.predict_calls += 1
        if self.predict_calls != 1:
            raise ValueError(
                "The fixed-answer oracle expects one complete native test batch"
            )
        if not X.index.equals(pd.RangeIndex(len(X))):
            raise ValueError("Native test index reset did not occur")
        query = self.output / "native_prediction_query"
        query.mkdir()
        values = X.astype(object).where(pd.notna(X), None)
        save(
            query / "test.json",
            {"columns": list(X.columns), "data": values.values.tolist()},
        )
        save(query / "metadata.json", self.metadata)
        prediction_path = self.output / "native_shuffled_predictions.json"
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                str(self.bundle / "predict.py"),
                str(query),
                str(prediction_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
            cwd=self.bundle,
            env={"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8"},
        )
        save(
            self.output / "predict_execution.json",
            {
                "return_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "query_rows": len(X),
                "query_columns": list(X.columns),
                "query_sha256": digest(query / "test.json"),
            },
        )
        if result.returncode:
            raise RuntimeError("Trusted oracle prediction command failed")
        value = json.loads(prediction_path.read_text())
        if (
            set(value) != {"classes", "predictions"}
            or value["classes"] != self.metadata["classes"]
        ):
            raise ValueError("Oracle output class schema mismatch")
        predicted = np.asarray(value["predictions"], dtype=np.float64)
        width = len(self.metadata["classes"]) or 1
        if predicted.shape != (len(X), width) or not np.isfinite(predicted).all():
            raise ValueError("Oracle output matrix mismatch")
        return predicted

    def _predict(self, X):
        return pd.Series(self._respond(X)[:, 0], index=X.index)

    def _predict_proba(self, X):
        return pd.DataFrame(
            self._respond(X), columns=self.metadata["classes"], index=X.index
        )


def run(args):
    task, output = args.task_dir.resolve(), args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    metadata = json.loads((task / "environment/data/metadata.json").read_text())
    if metadata["upstream_commit"] != PIN:
        raise ValueError("Task native-source pin differs from this validation script")
    native_root = Path(inspect.getfile(ExternalSystemModel)).resolve().parents[2]
    actual_sources = {name: digest(native_root / name) for name in NATIVE_SOURCE_HASHES}
    if actual_sources != NATIVE_SOURCE_HASHES:
        raise ValueError(
            "Imported native lifecycle/scorer files do not match the pinned source"
        )
    train = frame_from_json(task / "environment/data/train.json", metadata)
    features = [item["name"] for item in metadata["features"]]
    X, y = train[features], train[metadata["target"]]
    X_test = frame_from_json(task / "tests/test.json", metadata)
    y_test = pd.Series(json.loads((task / "tests/test_labels.json").read_text()))
    scorer = get_metric(metadata["metric"], problem_type=metadata["problem_type"])
    cleaner = LabelCleaner.construct(problem_type=metadata["problem_type"], y=y)
    system = KnownAnswerOracle(
        task_dir=task,
        output=output,
        metadata=metadata,
        training_X=X,
        training_y=y,
        problem_type=metadata["problem_type"],
        eval_metric=scorer,
        validation_metadata={"target_name": metadata["target"]},
        fit_kwargs={"num_cpus": 2, "num_gpus": 0, "memory_limit": 4, "time_limit": 600},
    )
    try:
        result = system.fit_custom(X=X, y=y, X_test=X_test, split_seed=metadata["seed"])
        error = evaluate(
            y_true=y_test,
            y_pred=result["predictions"],
            y_pred_proba=result["probabilities"],
            scorer=scorer,
            problem_type=metadata["problem_type"],
            label_cleaner=cleaner,
        )
        probabilities = result["probabilities"]
        values = (
            probabilities.to_numpy()
            if probabilities is not None
            else result["predictions"].to_numpy().reshape(-1, 1)
        )
        ideal = (
            pd.DataFrame(
                [
                    [float(label == cls) for cls in metadata["classes"]]
                    for label in y_test
                ],
                columns=metadata["classes"],
                dtype=np.float64,
            )
            if metadata["classes"]
            else None
        )
        optimum = evaluate(
            y_true=y_test,
            y_pred=y_test,
            y_pred_proba=ideal,
            scorer=scorer,
            problem_type=metadata["problem_type"],
            label_cleaner=cleaner,
        )
        audit = {
            "status": "scored",
            "role": "known-answer oracle through native ExternalSystemModel",
            "native_error": float(error),
            "native_optimum_error": float(optimum),
            "metric_optimal": int(error == optimum),
            "native_metric": metadata["metric"],
            "metric_direction": "lower_is_better",
            "n_test": len(y_test),
            "classes": metadata["classes"],
            "native_source_pin": PIN,
            "native_source_files": actual_sources,
            "input_audit": {
                "train_rows": len(X),
                "test_rows": len(X_test),
                "train_missing_feature_values": int(X.isna().sum().sum()),
                "test_missing_feature_values": int(X_test.isna().sum().sum()),
                "feature_dtypes": {
                    name: str(dtype) for name, dtype in X.dtypes.items()
                },
            },
            "source_module": ExternalSystemModel.__module__,
            "scorer_module": evaluate.__module__,
            "autogluon_core_version": importlib.metadata.version("autogluon.core"),
            "fit_custom": {
                key: value
                for key, value in result.items()
                if key not in ("predictions", "probabilities")
            },
            "test_shuffle": {
                "enabled": system.shuffle_test,
                "seed": system.shuffle_seed,
                "reset_index": system.reset_index_test,
                "native_restoration": True,
            },
            "claims": "Oracle correctness only; no model learning, containment equivalence, paper reproduction or formal parity claim",
        }
        if args.harbor_verifier_dir:
            harbor = args.harbor_verifier_dir.resolve()
            prior = json.loads((harbor / "result.json").read_text())
            reference = np.asarray(
                json.loads((harbor / "predictions.json").read_text()), dtype=np.float64
            )
            audit["harbor_comparison"] = {
                "verifier_directory": str(harbor),
                "exact_prediction_equality": bool(np.array_equal(values, reference)),
                "error_absolute_difference": abs(float(error) - prior["native_error"]),
                "harbor_metric_optimal": prior["metric_optimal"],
            }
            if (
                not audit["harbor_comparison"]["exact_prediction_equality"]
                or audit["harbor_comparison"]["error_absolute_difference"] > 1e-12
            ):
                raise AssertionError("Native and Harbor frozen oracle outputs disagree")
        save(output / "predictions.json", values.tolist())
        save(output / "native_result.json", audit)
        if not audit["metric_optimal"]:
            raise AssertionError(
                "Known-answer oracle did not attain the native metric optimum"
            )
        print(
            json.dumps(
                {
                    key: audit[key]
                    for key in (
                        "status",
                        "native_error",
                        "native_optimum_error",
                        "metric_optimal",
                        "n_test",
                    )
                }
            )
        )
    except BaseException as exc:
        save(output / "failure.json", {"type": type(exc).__name__, "message": str(exc)})
        raise
    finally:
        system.cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--harbor-verifier-dir", type=Path)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
