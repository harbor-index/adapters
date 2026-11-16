import hashlib
import importlib.util
from pathlib import Path
import unittest

from codeclash.catalog import ARENAS, config_for, config_sha256, get_arena


BUILDER_PATH = Path(__file__).resolve().parents[1] / "scripts/build_image.py"
_SPEC = importlib.util.spec_from_file_location(
    "codeclash_image_builder_test", BUILDER_PATH
)
builder = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(builder)


class CatalogTests(unittest.TestCase):
    def test_complete_registry_excludes_only_dummy(self):
        self.assertEqual(
            set(ARENAS),
            {
                "ants",
                "battlecode23",
                "battlecode24",
                "battlecode25",
                "battlesnake",
                "bomberland",
                "bridge",
                "chess",
                "corewar",
                "cyborg",
                "figgie",
                "gomoku",
                "halite",
                "halite2",
                "halite3",
                "huskybench",
                "lightcycles",
                "paintvolley",
                "robocode",
                "robotrumble",
                "scml",
            },
        )
        published = {
            slug
            for slug, entry in ARENAS.items()
            if entry["config_origin"] == "published_pvp"
        }
        self.assertEqual(
            published,
            {
                "battlesnake",
                "corewar",
                "halite",
                "huskybench",
                "robocode",
                "robotrumble",
            },
        )

    def test_benchmark_preserves_snapshots_and_diagnostic_players(self):
        for slug, entry in ARENAS.items():
            with self.subTest(arena=slug):
                config = config_for(slug, "benchmark")
                self.assertEqual(config_sha256(config), entry["native_config_sha256"])
                self.assertEqual(config["game"]["name"], entry["name"])
                self.assertIn("game_description", config["prompts"])
                self.assertNotIn("!include", str(config))
                if entry["config_origin"] == "upstream_diagnostic":
                    self.assertTrue(
                        all(p["agent"] == "dummy" for p in config["players"])
                    )
                config["players"][0]["name"] = "modified"
                self.assertNotEqual(config, config_for(slug, "benchmark"))
                copied = get_arena(slug)
                copied["native_config"]["game"]["name"] = "modified"
                self.assertEqual(
                    get_arena(slug)["native_config"]["game"]["name"], entry["name"]
                )

    def test_smoke_changes_only_run_budget(self):
        for slug in ARENAS:
            with self.subTest(arena=slug):
                smoke = config_for(slug, "smoke")
                native = config_for(slug, "benchmark")
                self.assertEqual(smoke["tournament"]["rounds"], 2)
                self.assertGreater(smoke["game"]["sims_per_round"], 0)
                self.assertLessEqual(
                    smoke["game"]["sims_per_round"], native["game"]["sims_per_round"]
                )
                expected = config_for(slug, "benchmark")
                expected["tournament"]["rounds"] = 2
                expected["game"]["sims_per_round"] = ARENAS[slug]["smoke_sims"]
                expected["game"]["sim_concurrency"] = 1
                self.assertEqual(smoke, expected)
        self.assertEqual(
            config_for("bomberland", "smoke")["game"]["sims_per_round"] % 2, 0
        )
        self.assertEqual(config_for("robocode", "smoke")["game"]["sims_per_round"], 10)
        self.assertEqual(len(config_for("bridge", "smoke")["players"]), 4)
        self.assertEqual(len(config_for("figgie", "smoke")["players"]), 4)

    def test_source_pins_and_edit_paths(self):
        for slug, entry in ARENAS.items():
            with self.subTest(arena=slug):
                self.assertRegex(entry["source_config_sha256"], r"^[0-9a-f]{64}$")
                self.assertFalse(Path(entry["smoke_edit_path"]).is_absolute())
                self.assertNotIn("..", Path(entry["smoke_edit_path"]).parts)
                workspace = entry["workspace_source"]
                if workspace["kind"] == "bundled_runtime":
                    self.assertIn(slug, {"bomberland", "cyborg"})
                    self.assertRegex(workspace["tree"], r"^[0-9a-f]{40}$")
                for source in [workspace, *entry["external_sources"]]:
                    self.assertRegex(source["commit"], r"^[0-9a-f]{40}$")
                    if source["kind"] == "repository":
                        self.assertTrue(
                            source["repository"].startswith("https://github.com/")
                        )
                        self.assertIn(
                            "credentials disabled", source["public_access"]["method"]
                        )

    def test_unknown_arena_or_profile_rejected(self):
        for slug in ["dummy", "BattleSnake", "../battlesnake", "missing"]:
            with self.assertRaises(ValueError):
                get_arena(slug)
        with self.assertRaises(ValueError):
            config_for("battlesnake", "tiny")


class ImageRecipeTests(unittest.TestCase):
    def test_default_tag_uses_shared_upstream_pin(self):
        self.assertEqual(
            builder.default_image("bridge"),
            f"codeclash-adapter/bridge:{builder.catalog.UPSTREAM_COMMIT[:12]}",
        )

    def test_prebuilt_robotrumble_engine_requires_amd64_image(self):
        self.assertEqual(builder.image_platform("robotrumble"), "linux/amd64")
        self.assertIsNone(builder.image_platform("battlesnake"))

    def test_java_battlecode_resolves_full_native_build_before_offline_runtime(self):
        for slug in ("battlecode23", "battlecode24"):
            entry = get_arena(slug)
            source = (
                f"RUN git clone {entry['workspace_source']['repository']} /workspace\n"
            )
            result = self.recipe(source, entry)
            self.assertIn("RUN ./gradlew --no-daemon clean build", result)
            self.assertIn("RUN ./gradlew --offline --no-daemon clean build", result)
        self.assertEqual(builder.runtime_preparation("battlecode25"), [])

    def test_halite3_caches_native_submission_crates_for_offline_compilation(self):
        self.assertEqual(
            builder.runtime_preparation("halite3"),
            [
                "cd submission && cargo build",
                "cd submission && cargo clean && cargo build --offline",
            ],
        )

    def test_huskybench_installs_eval7_source_build_prerequisite_first(self):
        entry = get_arena("huskybench")
        source = (
            f"RUN git clone {entry['workspace_source']['repository']} /workspace\n"
            "RUN pip install -r engine/requirements.txt\n"
        )
        result = self.recipe(source, entry)
        self.assertLess(
            result.index("RUN pip install Cython"),
            result.index("RUN pip install -r engine/requirements.txt"),
        )

    def test_halite2_metadata_mirror_keeps_native_bootstrap_and_selection(self):
        entry = get_arena("halite2")
        source = (
            f"RUN git clone {entry['workspace_source']['repository']} /workspace\n"
            "RUN curl --proto '=https' --tlsv1.2 -sSf https://get-ghcup.haskell.org | "
            "BOOTSTRAP_HASKELL_NONINTERACTIVE=1 sh\n"
        )
        result = self.recipe(source, entry)
        self.assertIn(
            "BOOTSTRAP_HASKELL_YAML=file:///opt/harbor-build-assets/ghcup-0.1.0.yaml",
            result,
        )
        self.assertIn("BOOTSTRAP_HASKELL_NONINTERACTIVE=1 sh", result)

    def recipe(self, text, entry=None):
        entry = get_arena("chess") if entry is None else entry
        entry["dockerfile_sha256"] = hashlib.sha256(text.encode()).hexdigest()
        return builder.pinned_dockerfile(text, entry)

    def test_workspace_and_auxiliary_checkout_precede_build(self):
        source = (
            "FROM ubuntu:22.04\n"
            "RUN git clone https://github.com/Babak-SSH/Kojiro.git /workspace \\\n"
            " && cd /workspace && make native\n"
            "RUN git clone https://github.com/Disservin/fastchess.git /tmp/fastchess \\\n"
            " && cd /tmp/fastchess && make\n"
        )
        output = self.recipe(source)
        entry = get_arena("chess")
        self.assertLess(
            output.index(entry["workspace_source"]["commit"]),
            output.index("make native"),
        )
        self.assertLess(
            output.index(entry["external_sources"][0]["commit"]),
            output.index("&& make\n"),
        )
        self.assertIn('org.harbor.codeclash.arena="chess"', output)
        self.assertIn(builder.PROVENANCE_PATH, output)

    def test_uncatalogued_clone_and_source_drift_fail_closed(self):
        entry = get_arena("chess")
        with self.assertRaisesRegex(ValueError, "differs"):
            builder.pinned_dockerfile("FROM ubuntu:latest\n", entry)
        with self.assertRaisesRegex(ValueError, "Unexpected"):
            self.recipe(
                "RUN git clone https://github.com/other/project.git /workspace\n"
            )
        with self.assertRaisesRegex(ValueError, "Not every"):
            self.recipe(
                "RUN git clone https://github.com/Babak-SSH/Kojiro.git /workspace\n"
            )

    def test_bundled_runtime_copy_remains_native(self):
        entry = get_arena("cyborg")
        source = (
            "RUN git clone https://github.com/CodeClash-ai/CybORG.git /opt/CybORG \\\n"
            " && cd /opt/CybORG && pip install -e .\n"
            "COPY codeclash/arenas/cyborg/runtime/ /workspace/\n"
            'RUN git init && git add . && git commit -m "Initial CybORG workspace"\n'
        )
        result = self.recipe(source, entry)
        self.assertIn("COPY codeclash/arenas/cyborg/runtime/ /workspace/", result)
        self.assertIn(
            f'org.codeclash.workspace-tree="{entry["workspace_source"]["tree"]}"',
            result,
        )


if __name__ == "__main__":
    unittest.main()
