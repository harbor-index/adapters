"""Pinned TabArena manifest and lazy OpenML assets; no model libraries required."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import arff

DATA_DIR = Path(__file__).parent / "data"
UPSTREAM_COMMIT = "bfa8c50c17885e5b722d75ce5bcb5db3822257a3"


def load_manifest() -> dict[str, Any]:
    """Official Lite selection: native split zero, repeat zero, fold zero."""
    manifest = json.loads((DATA_DIR / "manifest.json").read_text())
    if manifest["upstream_commit"] != UPSTREAM_COMMIT:
        raise ValueError("Manifest upstream pin does not match the adapter")
    splits = manifest["splits"]
    if (
        len(splits) != 51
        or len({item["openml_dataset_id"] for item in splits}) != 51
        or len({item["openml_task_id"] for item in splits}) != 51
        or any(int(item["repeat"]) != 0 or int(item["fold"]) != 0 for item in splits)
    ):
        raise ValueError("Manifest must contain exactly 51 official first splits")
    return manifest


def default_cache_dir() -> Path:
    """Keep downloaded source data outside generated dataset repositories."""
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".cache"
    return base / "harbor" / "tabarena"


def task_slug(split: dict[str, Any]) -> str:
    return f"tabarena-v01-{int(split['openml_task_id'])}-r{int(split['repeat'])}-f{int(split['fold'])}"


def digest(path: Path, algorithm: str = "sha256") -> str:
    value = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


class AssetCache:
    """Verify every cached asset before use; fetch only selected task assets.

    All 51 datasets' metadata snapshots are bundled with their original bytes
    and SHA256 pins. Raw datasets and split files are downloaded lazily and
    checked against available pins and OpenML MD5 checksums.
    """

    def __init__(self, directory: Path, max_download_bytes: int = 512 * 1024 * 1024):
        self.directory = Path(directory)
        self.max_download_bytes = max_download_bytes
        self.pins = json.loads((DATA_DIR / "asset_pins.json").read_text())
        bundle_path = DATA_DIR / "metadata_snapshots.json"
        self.snapshots: dict[str, str] = {}
        if bundle_path.is_file():
            bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
            if bundle["format_version"] != 1 or bundle["encoding"] != "utf-8":
                raise ValueError("Unsupported metadata snapshot bundle")
            self.snapshots = bundle["snapshots"]
        self.records: dict[str, dict[str, Any]] = {}

    def get(self, name: str, url: str, md5: str | None = None) -> Path:
        if Path(name).name != name:
            raise ValueError("Asset cache names must be plain filenames")
        path, vendor = self.directory / name, DATA_DIR / name
        expected = self.pins.get(name, {}).get("sha256")
        if vendor.is_file():
            path = vendor
        elif name in self.snapshots:
            content = self.snapshots[name].encode("utf-8")
            if not expected or hashlib.sha256(content).hexdigest() != expected:
                raise ValueError(f"SHA256 mismatch for bundled metadata: {name}")
            if not path.is_file() or digest(path) != expected:
                self.directory.mkdir(parents=True, exist_ok=True)
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(
                        dir=self.directory, delete=False
                    ) as output:
                        temporary = Path(output.name)
                        output.write(content)
                    os.replace(temporary, path)
                    temporary = None
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
        elif not path.exists():
            self.directory.mkdir(parents=True, exist_ok=True)
            temporary: Path | None = None
            try:
                request = urllib.request.Request(
                    url, headers={"User-Agent": "harbor-tabarena-adapter/0.1"}
                )
                with urllib.request.urlopen(request, timeout=120) as response:
                    with tempfile.NamedTemporaryFile(
                        dir=self.directory, delete=False
                    ) as output:
                        temporary = Path(output.name)
                        count = 0
                        while chunk := response.read(1024 * 1024):
                            count += len(chunk)
                            if count > self.max_download_bytes:
                                raise ValueError(
                                    f"Asset exceeds download limit: {name}"
                                )
                            output.write(chunk)
                if expected and digest(temporary) != expected:
                    raise ValueError(f"SHA256 mismatch for {name}")
                if md5 and digest(temporary, "md5") != md5:
                    raise ValueError(f"OpenML MD5 mismatch for {name}")
                os.replace(temporary, path)
                temporary = None
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        actual = digest(path)
        if expected and actual != expected:
            raise ValueError(f"SHA256 mismatch for {name}")
        if md5 and digest(path, "md5") != md5:
            raise ValueError(f"OpenML MD5 mismatch for {name}")
        self.records[name] = {
            "url": self.pins.get(name, {}).get("url", url),
            "sha256": actual,
            "bytes": path.stat().st_size,
            "sha256_pre_pinned": expected is not None,
        }
        if md5:
            self.records[name]["openml_md5"] = md5
        return path

    def get_json(self, name: str, url: str) -> dict[str, Any]:
        return json.loads(self.get(name, url).read_text())


@dataclass
class TaskData:
    metadata: dict[str, Any]
    train_rows: list[list[Any]]
    test_rows: list[list[Any]]
    test_labels: list[Any]
    provenance: dict[str, Any]


def _read_arff(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        return arff.load(stream)


def _read_split_indices(path: Path, split: dict[str, Any]) -> dict[str, list[int]]:
    """Stream the full OpenML split file, retaining only the selected outer fold."""
    selected: dict[str, list[int]] = {"TRAIN": [], "TEST": []}
    with path.open(encoding="utf-8") as stream:
        split_data = arff.load(stream, return_type=arff.DENSE_GEN)
        names = [name.lower() for name, _ in split_data["attributes"]]
        for row in split_data["data"]:
            item = dict(zip(names, row, strict=True))
            if (
                int(item["repeat"]) == int(split["repeat"])
                and int(item["fold"]) == int(split["fold"])
                and int(item.get("sample", 0)) == 0
            ):
                if item["type"] not in selected:
                    raise ValueError(f"Unexpected split partition: {item['type']}")
                index = int(item["rowid"])
                if index != item["rowid"]:
                    raise ValueError("Non-integral row ID")
                selected[item["type"]].append(index)
    return selected


def load_task(split: dict[str, Any], cache: AssetCache) -> TaskData:
    """Use OpenML's exact outer indices in their published order."""
    task_id, dataset_id = int(split["openml_task_id"]), int(split["openml_dataset_id"])
    task = cache.get_json(
        f"openml_task_{task_id}.json",
        f"https://www.openml.org/api/v1/json/task/{task_id}",
    )["task"]
    inputs = {item["name"]: item for item in task["input"]}
    source = inputs["source_data"]["data_set"]
    if (
        int(source["data_set_id"]) != dataset_id
        or source["target_feature"] != split["target"]
    ):
        raise ValueError("OpenML task identity/target differs from the pinned manifest")
    description = cache.get_json(
        f"openml_data_{dataset_id}.json",
        f"https://www.openml.org/api/v1/json/data/{dataset_id}",
    )["data_set_description"]
    if int(description["id"]) != dataset_id:
        raise ValueError("OpenML dataset ID mismatch")
    feature_metadata = cache.get_json(
        f"features_{dataset_id}.json",
        f"https://www.openml.org/api/v1/json/data/features/{dataset_id}",
    )["data_features"]["feature"]
    data_path = cache.get(
        f"dataset_{dataset_id}.arff",
        description["url"],
        description.get("md5_checksum"),
    )
    split_url = inputs["estimation_procedure"]["estimation_procedure"][
        "data_splits_url"
    ]
    splits_path = cache.get(f"splits_{task_id}.arff", split_url)
    dataset = _read_arff(data_path)
    rows, attributes = dataset["data"], dataset["attributes"]
    names = [attribute[0] for attribute in attributes]
    if len(names) != len(set(names)):
        raise ValueError("Duplicate feature names cannot be represented unambiguously")
    target = split["target"]
    target_index = names.index(target)
    features, feature_indices = [], []
    for item in sorted(feature_metadata, key=lambda item: int(item["index"])):
        if item["name"] == target or any(
            str(item.get(flag, "false")).lower() == "true"
            for flag in ["is_ignore", "is_row_identifier", "is_target"]
        ):
            continue
        index = int(item["index"])
        if names[index] != item["name"]:
            raise ValueError("Feature ordering disagrees with OpenML metadata")
        kind = (
            "numeric"
            if item["data_type"] in {"numeric", "integer", "real"}
            else "categorical"
        )
        feature = {"name": item["name"], "type": kind}
        if isinstance(attributes[index][1], list):
            # ARFF's nominal declaration is the authoritative category space;
            # preserve it separately from observed train/test values.
            feature["categories"] = attributes[index][1]
        features.append(feature)
        feature_indices.append(index)
    if len(features) != int(split["feature_count"]):
        raise ValueError("Feature count differs from the pinned TabArena manifest")
    selected = _read_split_indices(splits_path, split)
    train_indices, test_indices = selected["TRAIN"], selected["TEST"]
    train_set, test_set = set(train_indices), set(test_indices)
    if not train_set or not test_set or train_set & test_set:
        raise ValueError("Missing or overlapping outer train/test split")
    if len(train_set) != len(train_indices) or len(test_set) != len(test_indices):
        raise ValueError("Duplicate row indices in an outer split")
    if train_set | test_set != set(range(len(rows))):
        raise ValueError("Outer split does not partition the dataset exactly")
    y_train = [rows[index][target_index] for index in train_indices]
    y_test = [rows[index][target_index] for index in test_indices]
    if any(value is None for value in y_train + y_test):
        raise ValueError("Missing target values are unsupported")
    classes = sorted(set(y_train)) if split["problem_type"] != "regression" else []
    if classes and not set(y_test).issubset(classes):
        raise ValueError("Test classes absent from the training label space")
    metadata = {
        "task_id": task_id,
        "dataset_id": dataset_id,
        "dataset_name": split["dataset_name"],
        "problem_type": split["problem_type"],
        "metric": split["metric"],
        "target": target,
        "features": features,
        "classes": classes,
        "train_rows": len(train_indices),
        "test_rows": len(test_indices),
        "repeat": int(split["repeat"]),
        "fold": int(split["fold"]),
        "seed": 0,
        "upstream_commit": UPSTREAM_COMMIT,
        "license": description.get("licence"),
        "citation": description.get("citation"),
    }
    asset_names = [
        f"openml_task_{task_id}.json",
        f"openml_data_{dataset_id}.json",
        f"features_{dataset_id}.json",
        f"dataset_{dataset_id}.arff",
        f"splits_{task_id}.arff",
    ]
    provenance = {
        "task_name": f"tabarena/{task_slug(split)}",
        "upstream_commit": UPSTREAM_COMMIT,
        "split": split,
        "train_indices": train_indices,
        "test_indices": test_indices,
        "assets": {name: cache.records[name] for name in asset_names},
    }
    return TaskData(
        metadata=metadata,
        train_rows=[
            [rows[index][column] for column in feature_indices]
            + [rows[index][target_index]]
            for index in train_indices
        ],
        test_rows=[
            [rows[index][column] for column in feature_indices]
            for index in test_indices
        ],
        test_labels=y_test,
        provenance=provenance,
    )
