"""Offline generation, provenance and data-boundary regression checks."""

from __future__ import annotations

import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tabarena.adapter import TabArenaAdapter
from tabarena.data import (
    AssetCache,
    load_manifest,
    default_cache_dir,
    load_task,
    task_slug,
)


class ManifestTests(unittest.TestCase):
    def test_all_bundled_metadata_is_available_offline_with_original_hashes(self):
        lite = load_manifest()
        expected = set()
        for split in lite["splits"]:
            expected.update(
                {
                    f"openml_task_{split['openml_task_id']}.json",
                    f"openml_data_{split['openml_dataset_id']}.json",
                    f"features_{split['openml_dataset_id']}.json",
                }
            )
        with tempfile.TemporaryDirectory() as directory:
            cache = AssetCache(Path(directory))
            self.assertEqual(set(cache.snapshots), expected)
            self.assertEqual(len(expected), 153)
            with patch(
                "urllib.request.urlopen",
                side_effect=AssertionError("network forbidden"),
            ):
                for name in sorted(expected):
                    self.assertIsInstance(cache.get_json(name, "https://unused"), dict)
                    raw = (Path(directory) / name).read_bytes()
                    self.assertEqual(
                        hashlib.sha256(raw).hexdigest(), cache.pins[name]["sha256"]
                    )
                    self.assertEqual(cache.records[name]["bytes"], len(raw))

    def test_official_lite_scope_matches_pinned_native_selection(self):
        lite = load_manifest()
        selected = load_manifest()["splits"]
        self.assertEqual(len(selected), 51)
        self.assertEqual(len({x["openml_dataset_id"] for x in selected}), 51)
        self.assertEqual(
            [task_slug(x) for x in selected],
            [task_slug(x) for x in lite["splits"]],
        )
        self.assertTrue(all(x["split_index"] == "r0f0" for x in selected))
        self.assertEqual(
            {x["problem_type"] for x in selected},
            {"binary", "multiclass", "regression"},
        )

    def test_selection_and_invalid_requests(self):
        adapter = TabArenaAdapter(Path("unused"), limit=2)
        self.assertEqual(len(adapter.select_tasks()), 2)
        name = task_slug(adapter.select_tasks()[1])
        selected = TabArenaAdapter(Path("unused"), task_ids=["tabarena/" + name])
        self.assertEqual([task_slug(x) for x in selected.select_tasks()], [name])
        with self.assertRaisesRegex(ValueError, "Unknown task IDs"):
            TabArenaAdapter(Path("unused"), task_ids=["../bad"]).select_tasks()
        with self.assertRaises(ValueError):
            TabArenaAdapter(Path("unused"), limit=0)

    def test_cache_is_outside_dataset_repository(self):
        with patch.dict("os.environ", {"XDG_CACHE_HOME": "/tmp/example-cache"}):
            adapter = TabArenaAdapter(Path("/tmp/repository/datasets/tabarena"))
            self.assertEqual(
                adapter.cache.directory, Path("/tmp/example-cache/harbor/tabarena")
            )
            explicit = TabArenaAdapter(
                Path("/tmp/out"), cache_dir=Path("/tmp/explicit")
            )
            self.assertEqual(explicit.cache.directory, Path("/tmp/explicit"))
        with (
            patch.dict("os.environ", {"XDG_CACHE_HOME": "relative"}),
            patch("pathlib.Path.home", return_value=Path("/tmp/user")),
        ):
            self.assertEqual(
                default_cache_dir(), Path("/tmp/user/.cache/harbor/tabarena")
            )

    def test_subset_metric_does_not_hide_missing_or_excess_trials(self):
        import runpy

        summarize = runpy.run_path(
            str(Path(__file__).parents[1] / "scripts/oracle_coverage_metric.py")
        )["summarize"]
        success = {"metric_optimal": 1, "native_error": 0.0}
        self.assertEqual(summarize([success], 2)["exact_optimum_fraction"], 0.5)
        self.assertEqual(summarize([success], 1)["complete_scored_count"], 1)
        self.assertEqual(summarize([success, None], 2)["complete_scored_count"], 0)
        self.assertEqual(summarize([success, success], 1)["exact_optimum_fraction"], 0)
        self.assertEqual(summarize([success] * 51)["exact_optimum_fraction"], 1)
        with self.assertRaises(ValueError):
            summarize([], 0)

    def test_manifest_only_is_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out"
            with patch(
                "urllib.request.urlopen",
                side_effect=AssertionError("network forbidden"),
            ):
                TabArenaAdapter(output, manifest_only=True).run()
            manifest = json.loads((output / "task_manifest.json").read_text())
            self.assertEqual(manifest["selected_task_count"], 51)
            self.assertEqual(len(list(output.iterdir())), 1)


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.assets = self.root / "assets"
        self.assets.mkdir()
        self.template = self.root / "template"
        for name in ["environment", "tests", "solution"]:
            (self.template / name).mkdir(parents=True)
        (self.template / "task.toml").write_text(
            'version="1.0"\n[task]\nname="$task_name"\n'
        )
        (self.template / "instruction.md").write_text(
            "Train $dataset_name with $train_rows rows; classes $classes_json.\n"
        )
        (self.template / "environment/Dockerfile").write_text(
            "FROM python:3.12-slim\nCOPY data /workspace/data\n"
        )
        (self.template / "tests/test.sh").write_text("#!/bin/sh\nexit 0\n")
        (self.template / "solution/solve.sh").write_text("#!/bin/sh\nexit 0\n")
        self.split = {
            "openml_task_id": 12,
            "openml_dataset_id": 34,
            "repeat": 0,
            "fold": 0,
            "dataset_name": "synthetic",
            "target": "target",
            "problem_type": "binary",
            "metric": "roc_auc",
            "feature_count": 1,
        }
        self.write_asset(
            "dataset_34.arff",
            "@relation data\n@attribute x numeric\n@attribute target {No,Yes}\n@data\n10,No\n20,Yes\n30,No\n40,Yes\n",
        )
        self.write_asset(
            "splits_12.arff",
            "@relation splits\n@attribute type {TRAIN,TEST}\n@attribute rowid numeric\n@attribute repeat numeric\n@attribute fold numeric\n@data\nTRAIN,1,0,0\nTRAIN,0,0,0\nTEST,3,0,0\nTEST,2,0,0\n",
        )
        task = {
            "task": {
                "input": [
                    {
                        "name": "source_data",
                        "data_set": {"data_set_id": "34", "target_feature": "target"},
                    },
                    {
                        "name": "estimation_procedure",
                        "estimation_procedure": {
                            "data_splits_url": "https://example.invalid/splits"
                        },
                    },
                ]
            }
        }
        data = {
            "data_set_description": {
                "id": "34",
                "url": "https://example.invalid/data",
                "md5_checksum": hashlib.md5(
                    (self.assets / "dataset_34.arff").read_bytes()
                ).hexdigest(),
                "licence": "CC0",
            }
        }
        features = {
            "data_features": {
                "feature": [
                    {"index": "0", "name": "x", "data_type": "numeric"},
                    {
                        "index": "1",
                        "name": "target",
                        "data_type": "nominal",
                        "is_target": "true",
                    },
                ]
            }
        }
        for name, content in [
            ("openml_task_12.json", task),
            ("openml_data_34.json", data),
            ("features_34.json", features),
        ]:
            self.write_asset(name, json.dumps(content))
        self.refresh_pins()
        self.data_patch = patch("tabarena.data.DATA_DIR", self.assets)
        self.data_patch.start()

    def tearDown(self):
        self.data_patch.stop()
        self.temporary.cleanup()

    def write_asset(self, name, content):
        (self.assets / name).write_text(content)

    def refresh_pins(self):
        pins = {
            p.name: {"sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in self.assets.iterdir()
            if p.name != "asset_pins.json"
        }
        (self.assets / "asset_pins.json").write_text(json.dumps(pins))

    def bundle_metadata(self):
        snapshots = {}
        for name in ["openml_task_12.json", "openml_data_34.json", "features_34.json"]:
            path = self.assets / name
            snapshots[name] = path.read_bytes().decode("utf-8")
            path.unlink()
        (self.assets / "metadata_snapshots.json").write_text(
            json.dumps(
                {"format_version": 1, "encoding": "utf-8", "snapshots": snapshots}
            )
        )

    def test_bundling_preserves_every_generated_file(self):
        manifest = {"upstream_commit": "fixture", "splits": [self.split]}
        with (
            patch("tabarena.adapter.load_manifest", return_value=manifest),
            patch("tabarena.adapter.TEMPLATE_DIR", self.template),
            patch(
                "urllib.request.urlopen",
                side_effect=AssertionError("network forbidden"),
            ),
        ):
            old = self.root / "old"
            new = self.root / "new"
            TabArenaAdapter(old, cache_dir=self.root / "cache").run()
            self.bundle_metadata()
            TabArenaAdapter(new, cache_dir=self.root / "cache").run()

        def contents(root):
            return {
                str(path.relative_to(root)): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }

        self.assertEqual(contents(old), contents(new))

    def test_corrupt_bundle_is_rejected_even_with_valid_cached_metadata(self):
        self.bundle_metadata()
        cache_dir = self.root / "cache"
        load_task(self.split, AssetCache(cache_dir))
        bundle_path = self.assets / "metadata_snapshots.json"
        bundle = json.loads(bundle_path.read_text())
        bundle["snapshots"]["openml_task_12.json"] += " "
        bundle_path.write_text(json.dumps(bundle))
        with self.assertRaisesRegex(ValueError, "SHA256 mismatch for bundled metadata"):
            load_task(self.split, AssetCache(cache_dir))

    def test_exact_native_order_and_separate_labels(self):
        with patch(
            "urllib.request.urlopen", side_effect=AssertionError("network forbidden")
        ):
            data = load_task(self.split, AssetCache(self.root / "cache"))
        self.assertEqual(data.train_rows, [[20.0, "Yes"], [10.0, "No"]])
        self.assertEqual(data.test_rows, [[40.0], [30.0]])
        self.assertEqual(data.test_labels, ["Yes", "No"])
        self.assertEqual(data.metadata["classes"], ["No", "Yes"])
        self.assertEqual(data.provenance["test_indices"], [3, 2])

    def test_corrupt_pinned_bytes_fail_closed(self):
        with (self.assets / "dataset_34.arff").open("a") as stream:
            stream.write("\n% changed\n")
        with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
            load_task(self.split, AssetCache(self.root / "cache"))

    def test_nominal_categories_missing_and_literal_na_are_preserved(self):
        self.split["feature_count"] = 2
        self.write_asset(
            "dataset_34.arff",
            "@relation data\n@attribute x numeric\n"
            "@attribute category {NA,nan,'',blue}\n"
            "@attribute target {No,Yes}\n@data\n"
            "10,NA,No\n20,nan,Yes\n30,'',No\n40,?,Yes\n",
        )
        self.write_asset(
            "features_34.json",
            json.dumps(
                {
                    "data_features": {
                        "feature": [
                            {"index": "0", "name": "x", "data_type": "numeric"},
                            {"index": "1", "name": "category", "data_type": "nominal"},
                            {
                                "index": "2",
                                "name": "target",
                                "data_type": "nominal",
                                "is_target": "true",
                            },
                        ]
                    }
                }
            ),
        )
        description = json.loads((self.assets / "openml_data_34.json").read_text())
        description["data_set_description"]["md5_checksum"] = hashlib.md5(
            (self.assets / "dataset_34.arff").read_bytes()
        ).hexdigest()
        self.write_asset("openml_data_34.json", json.dumps(description))
        self.refresh_pins()
        data = load_task(self.split, AssetCache(self.root / "cache"))
        self.assertEqual(data.train_rows, [[20.0, "nan", "Yes"], [10.0, "NA", "No"]])
        self.assertEqual(data.test_rows, [[40.0, None], [30.0, ""]])
        self.assertEqual(
            data.metadata["features"][1]["categories"], ["NA", "nan", "", "blue"]
        )

    def test_overlap_rejected_even_with_valid_hash(self):
        path = self.assets / "splits_12.arff"
        path.write_text(path.read_text().replace("TEST,3,0,0", "TEST,1,0,0"))
        self.refresh_pins()
        with self.assertRaisesRegex(ValueError, "overlapping"):
            load_task(self.split, AssetCache(self.root / "cache"))

    def test_generation_and_selective_overwrite(self):
        output = self.root / "out"
        manifest = {"upstream_commit": "fixture", "splits": [self.split]}
        with (
            patch("tabarena.adapter.load_manifest", return_value=manifest),
            patch("tabarena.adapter.TEMPLATE_DIR", self.template),
        ):
            adapter = TabArenaAdapter(output, cache_dir=self.root / "cache")
            adapter.run()
            task = output / "tabarena-v01-12-r0-f0"
            with (task / "environment/data/train.csv").open() as stream:
                self.assertEqual(
                    list(csv.reader(stream)),
                    [["x", "target"], ["20.0", "Yes"], ["10.0", "No"]],
                )
            with (task / "tests/test.csv").open() as stream:
                self.assertEqual(list(csv.reader(stream)), [["x"], ["40.0"], ["30.0"]])
            self.assertEqual(
                json.loads((task / "tests/test_labels.json").read_text()), ["Yes", "No"]
            )
            self.assertEqual(
                (task / "tests/metadata.json").read_bytes(),
                (task / "environment/data/metadata.json").read_bytes(),
            )
            self.assertEqual(
                {p.name for p in (task / "environment/data").iterdir()},
                {"metadata.json", "train.csv", "train.json"},
            )
            self.assertEqual(
                json.loads((task / "environment/data/train.json").read_text()),
                {"columns": ["x", "target"], "data": [[20.0, "Yes"], [10.0, "No"]]},
            )
            self.assertEqual(
                json.loads((task / "tests/test.json").read_text()),
                {"columns": ["x"], "data": [[40.0], [30.0]]},
            )
            self.assertTrue((task / "solution/answers.json").is_file())
            self.assertFalse((task / "environment/data/answers.json").exists())
            self.assertNotIn(
                "test_indices", (task / "environment/data/metadata.json").read_text()
            )
            self.assertIn(
                'name="tabarena/tabarena-v01-12-r0-f0"',
                (task / "task.toml").read_text(),
            )
            with self.assertRaises(FileExistsError):
                adapter.run()
            sibling = output / "unrelated"
            sibling.mkdir()
            (sibling / "keep").write_text("preserved")
            TabArenaAdapter(output, overwrite=True, cache_dir=self.root / "cache").run()
            self.assertEqual((sibling / "keep").read_text(), "preserved")


if __name__ == "__main__":
    unittest.main()
