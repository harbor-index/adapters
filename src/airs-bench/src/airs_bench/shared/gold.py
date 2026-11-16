"""Oracle: write the gold test labels as submission.csv (must score the optimum)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pandas as pd

here = Path(__file__).parent
cfg = json.loads((here / "score_config.json").read_text())
out_path = Path(sys.argv[1] if len(sys.argv) > 1 else "/workspace/submission.csv")

work = Path(tempfile.mkdtemp())
raw, data, logs = work / "raw", work / "data", work / "logs"
for d in (raw, data, logs):
    d.mkdir()
(logs / "submission.csv").write_text("")  # evaluate_prepare.py copies it; unused here

subprocess.run([sys.executable, str(here / "download_data.py"), "--out", str(raw)], check=True)
subprocess.run(
    [sys.executable, str(here / "evaluate_prepare.py"),
     "--global-shared-data-dir", str(raw),
     "--agent-data-mount-dir", str(data),
     "--agent-log-dir", str(logs)],
    check=True, cwd=here,
)

os.chdir(work)
sys.path.insert(0, str(here))
import evaluate as ev  # noqa: E402  (the task's original evaluate.py)

import ast  # noqa: E402
import json as _json  # noqa: E402

task_id = cfg.get("task_id", "")
header = cfg["header"]


def first_loader():
    for n in ("load_test_set", "load_test_answers"):
        if hasattr(ev, n):
            return getattr(ev, n)
    raise SystemExit("no gold-label loader in evaluate.py")


def write(df):
    df.to_csv(out_path, index=False)
    print(f"wrote {len(df)} rows to {out_path}")


if task_id == "reading-comprehension-squad-exact-match":
    # labels are lists of acceptable answers; submit one string each
    write(pd.DataFrame({header: [a[0] for a in ev.load_test_set()]}))
elif task_id == "question-answering-duo-rc-accuracy":
    answers, no_answers = ev.load_test_answers()
    has = [not bool(n) for n in no_answers]
    write(pd.DataFrame({
        "has_answer": has,
        "answer": [a[0] if h and len(a) else "" for a, h in zip(answers, has)],
    }))
elif task_id == "code-retrieval-code-x-glue-mrr":
    ds = ev.load_test_set()
    write(pd.DataFrame({
        "query": list(ds["query"]),
        "rankings": [_json.dumps([i]) for i in ds["id"]],
    }))
elif task_id in ("time-series-forecasting-solar-weekly-mae", "time-series-forecasting-kaggle-web-traffic-mase"):
    from datasets import load_from_disk
    full = load_from_disk("./data/test_with_labels")
    preds = []
    # stream in batches: materialising 145k long series at once needs several GB
    for batch in full.select_columns(["label_target", "target"]).iter(batch_size=500):
        for lab, tgt in zip(batch["label_target"], batch["target"]):
            window = [float(x) for x in lab[len(tgt):]]
            if task_id.endswith("kaggle-web-traffic-mase"):
                # evaluate.py expects full-length series and drops the first len(target)
                # points itself, so the training part can be filled with zeros.
                window = [0.0] * len(tgt) + window
            preds.append(_json.dumps(window))
    write(pd.DataFrame({header: preds}))
elif task_id == "question-answering-eli5-rouge-l":
    labels = ev.load_test_set()
    write(pd.DataFrame({header: [l[0] if isinstance(l, (list, tuple)) else l for l in labels]}))
elif task_id == "code-generation-apps-pass-at5":
    from datasets import load_from_disk
    raw_ds = load_from_disk(str(raw / "codeparrot/apps/all"))["test"]
    rows = []
    for ex in raw_ds:
        sols = _json.loads(ex["solutions"]) if ex["solutions"] else []
        sols = (sols + [""] * 5)[:5]
        rows.append({f"code{i + 1}": sols[i] for i in range(5)})
    write(pd.DataFrame(rows))
else:
    labels = first_loader()()
    write(pd.DataFrame({header: list(labels)}))

shutil.rmtree(work, ignore_errors=True)
