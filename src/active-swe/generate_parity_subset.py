"""Rebuild the proposed Recorded parity manifest from the pinned main split."""
import argparse
import random
from collections import Counter, defaultdict
from pathlib import Path

from active_swe.adapter import PARITY_IDS_FILE, load_records


def allocate(total, quotas, lower, upper):
    """Allocate integer slots within bounds, prioritizing the largest shortfall."""
    counts = lower.copy()
    if not sum(counts.values()) <= total <= sum(upper.values()):
        raise ValueError("Sample size is incompatible with the stratum bounds")
    while sum(counts.values()) < total:
        eligible = [key for key in sorted(counts) if counts[key] < upper[key]]
        counts[max(eligible, key=lambda key: quotas[key] - counts[key])] += 1
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", type=int, default=12)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--data-file", type=Path)
    args = parser.parse_args()
    records = sorted(load_records(args.data_file), key=lambda row: row.task_id)
    by_lang = Counter(row.language for row in records)
    strata = defaultdict(list)
    for row in records:
        strata[row.language, row.set].append(row.task_id)
    slots = allocate(args.size,
                     {k: args.size * n / len(records) for k, n in by_lang.items()},
                     dict.fromkeys(by_lang, 1), by_lang)
    n_hard = {k: len(strata[k, "hard"]) for k in by_lang}
    hard = allocate(round(args.size * sum(n_hard.values()) / len(records)),
                    {k: slots[k] * n_hard[k] / by_lang[k] for k in by_lang},
                    {k: max(0, slots[k] - len(strata[k, "simple"])) for k in by_lang},
                    {k: min(slots[k], n_hard[k]) for k in by_lang})
    rng = random.Random(args.seed)
    chosen = []
    for lang in sorted(slots):
        chosen.extend(rng.sample(strata[lang, "hard"], hard[lang]))
        chosen.extend(rng.sample(strata[lang, "simple"], slots[lang] - hard[lang]))
    PARITY_IDS_FILE.write_text("\n".join(sorted(chosen)) + "\n")


if __name__ == "__main__":
    main()
