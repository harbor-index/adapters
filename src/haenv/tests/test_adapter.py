"""Unit tests for HAEnv Harbor adapter stratified parity split and pin integrity."""

from __future__ import annotations

from collections import Counter
import random
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from haenv.adapter import (  # noqa: E402
    PARITY_SUBSET_SIZE,
    UPSTREAM_PIN,
    HealthAgentEnvironmentAdapter,
    select_parity_subset,
)


def _make_synthetic_pool() -> list[dict]:
    """Build a realistic 100-item M2 pool spanning frames F0-F3 and join types."""
    spec = [
        ("F0", "unified", 16),
        ("F0", "comorbidity", 10),
        ("F0", "independent", 9),
        ("F1", "unified", 12),
        ("F1", "comorbidity", 7),
        ("F1", "independent", 6),
        ("F2", "unified", 10),
        ("F2", "comorbidity", 6),
        ("F2", "independent", 4),
        ("F3", "unified", 10),
        ("F3", "comorbidity", 6),
        ("F3", "independent", 4),
    ]
    rows: list[dict] = []
    idx = 0
    for frame, join_gold, count in spec:
        for _ in range(count):
            idx += 1
            rows.append(
                {
                    "case_id": f"jd-{idx:04d}",
                    "frame": frame,
                    "join_gold": join_gold,
                }
            )
    assert len(rows) == 100
    return rows


class HAEnvParitySplitTests(unittest.TestCase):
    def test_upstream_pin_is_full_sha(self) -> None:
        self.assertRegex(UPSTREAM_PIN, r"^[0-9a-f]{40}$")
        self.assertTrue(UPSTREAM_PIN.startswith("94761fb"))

    def test_parity_subset_determinism_and_permutation_invariance(self) -> None:
        pool = _make_synthetic_pool()
        first = [r["case_id"] for r in select_parity_subset(pool)]
        second = [r["case_id"] for r in select_parity_subset(pool)]
        self.assertEqual(len(first), PARITY_SUBSET_SIZE)
        self.assertEqual(first, second)

        shuffled = list(pool)
        random.Random(42).shuffle(shuffled)
        from_shuffled = [r["case_id"] for r in select_parity_subset(shuffled)]
        self.assertEqual(set(first), set(from_shuffled))

    def test_parity_subset_stratification_balance(self) -> None:
        pool = _make_synthetic_pool()
        subset = select_parity_subset(pool, target_n=30)
        self.assertEqual(len(subset), 30)

        full_cells = Counter((r["frame"], r["join_gold"]) for r in pool)
        sub_cells = Counter((r["frame"], r["join_gold"]) for r in subset)
        for key, full_count in full_cells.items():
            expected = 30 * full_count / len(pool)
            self.assertLess(abs(sub_cells[key] - expected), 1.0, f"Stratum {key} imbalanced")

        for field in ("frame", "join_gold"):
            full_marginal = Counter(r[field] for r in pool)
            sub_marginal = Counter(r[field] for r in subset)
            for value, full_count in full_marginal.items():
                expected = 30 * full_count / len(pool)
                self.assertLessEqual(
                    abs(sub_marginal[value] - expected),
                    1.5,
                    f"Marginal {field}={value} got {sub_marginal[value]}, expected ~{expected:.2f}",
                )

    def test_parity_subset_small_pool_returns_all(self) -> None:
        pool = _make_synthetic_pool()[:12]
        subset = select_parity_subset(pool, target_n=30)
        self.assertEqual([r["case_id"] for r in subset], [r["case_id"] for r in pool])

    def test_adapter_run_respects_split_and_limit(self) -> None:
        pool = _make_synthetic_pool()
        expected_30 = [r["case_id"] for r in select_parity_subset(pool, target_n=30)]

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "out"
            rendered_parity: list[str] = []
            adapter = HealthAgentEnvironmentAdapter(output_dir=out_dir, split="parity", overwrite=True)
            with (
                mock.patch.object(adapter, "_ensure_pack_batch", return_value=(Path(tmp), None)),
                mock.patch.object(adapter, "_extract_cases", return_value=pool),
                mock.patch.object(adapter, "_render_task", side_effect=lambda r: rendered_parity.append(r["case_id"])),
            ):
                adapter.run()
            self.assertEqual(rendered_parity, expected_30)

            rendered_limited: list[str] = []
            adapter_lim = HealthAgentEnvironmentAdapter(output_dir=out_dir, split="parity", limit=5, overwrite=True)
            with (
                mock.patch.object(adapter_lim, "_ensure_pack_batch", return_value=(Path(tmp), None)),
                mock.patch.object(adapter_lim, "_extract_cases", return_value=pool),
                mock.patch.object(adapter_lim, "_render_task", side_effect=lambda r: rendered_limited.append(r["case_id"])),
            ):
                adapter_lim.run()
            self.assertEqual(rendered_limited, expected_30[:5])

            rendered_diag: list[str] = []
            adapter_diag = HealthAgentEnvironmentAdapter(output_dir=out_dir, split="diagnosis", overwrite=True)
            with (
                mock.patch.object(adapter_diag, "_ensure_pack_batch", return_value=(Path(tmp), None)),
                mock.patch.object(adapter_diag, "_extract_cases", return_value=pool),
                mock.patch.object(adapter_diag, "_render_task", side_effect=lambda r: rendered_diag.append(r["case_id"])),
            ):
                adapter_diag.run()
            self.assertEqual(len(rendered_diag), 100)


if __name__ == "__main__":
    unittest.main()
