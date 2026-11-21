"""Convert the broker's trace and run the pinned upstream SoPR evaluator."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

TESTS = Path(__file__).resolve().parent
SPLIT = "G1_instruction"
METHOD = "CoT@1"
MODEL_LABEL = "harbor"


def grade(trace_path, results):
    results.mkdir(parents=True, exist_ok=True)
    package = json.loads(trace_path.read_text())
    # Missing status identifies a partial or untrusted export from an older
    # trial. A transport error cannot be converted into a score even if an
    # earlier action had already produced a plausible answer.
    if "infrastructure_failure" not in package:
        raise ValueError("Missing broker collection status")
    if package["infrastructure_failure"] is not None:
        raise RuntimeError("Broker marked trial unscoreable: " + str(package["infrastructure_failure"]))
    query = json.loads((TESTS/"query.json").read_text())
    qid = str(query["query_id"])
    raw = package["raw"]
    if str(package["query_id"]) != qid or raw["answer_generation"]["query"] != query["query"]:
        raise ValueError("Collected broker artifact and verifier task disagree")

    with TemporaryDirectory() as staging:
        root = Path(staging)
        native = root/"raw"
        native.mkdir()
        (native/f"{qid}_{METHOD}.json").write_text(json.dumps(raw))
        ids = root/"ids"
        ids.mkdir()
        (ids/f"{SPLIT}.json").write_text(json.dumps({qid:query["query"]}))
        converted = root/"converted"/MODEL_LABEL
        converted.mkdir(parents=True)
        target = converted/f"{SPLIT}.json"
        def upstream(script, *args, env=None):
            subprocess.run([sys.executable,str(TESTS/"tooleval"/script),*map(str,args)],
                           check=True, env=env)
        upstream("convert_to_answer_format.py","--answer_dir",native,"--method",METHOD,"--output",target)
        data = json.loads(target.read_text())
        if set(data) != {qid}:
            raise ValueError("Upstream converter did not emit the requested ID")
        # Use upstream's own last-tool extraction. Missing Finish short-circuits
        # in eval_pass_rate.py before a model call and needs no credentials.
        sys.path.insert(0, str(TESTS/"tooleval"))
        from utils import get_steps
        _, final = get_steps(data[qid])
        final_answer = data[qid]["answer"]["final_answer"]
        requires_judge = ("'name': 'Finish'" in final and final_answer != ""
                          and "give_up_and_restart" not in final_answer)
        key = os.environ.get("STABLETOOLBENCH_JUDGE_API_KEY")
        url = os.environ.get("STABLETOOLBENCH_JUDGE_BASE_URL")
        model = os.environ.get("STABLETOOLBENCH_JUDGE_MODEL")
        if requires_judge and not all((key, url, model)):
            raise RuntimeError("Finished trace requires STABLETOOLBENCH_JUDGE_API_KEY, STABLETOOLBENCH_JUDGE_BASE_URL and STABLETOOLBENCH_JUDGE_MODEL")
        pool = root/"pool.json"
        pool.touch(mode=0o600)
        pool.write_text(json.dumps([{"api_key":key,"api_base":url}] if requires_judge else []))
        env = {**os.environ, "API_POOL_FILE":str(pool), "EVAL_MODEL":model or ""}
        env.pop("OPENAI_KEY", None)
        upstream("eval_pass_rate.py", "--converted_answer_path", root/"converted",
                 "--save_path", results, "--reference_model", MODEL_LABEL, "--test_ids", ids,
                 "--max_eval_threads", 1, "--evaluate_times", 3, "--test_set", SPLIT, "--overwrite",env=env)
        rows = json.loads((results/f"{SPLIT}_{MODEL_LABEL}.json").read_text())
        labels = rows[qid]["is_solved"]
        if set(labels) != {"0","1","2"}:
            raise ValueError("Incomplete upstream evaluations")
        values = {"AnswerStatus.Solved":1.0,"AnswerStatus.Unsure":0.5,"AnswerStatus.Unsolved":0.0}
        reward = sum(values[v] for v in labels.values())/len(labels)
        (results/"converted.json").write_text(json.dumps(data, ensure_ascii=False))
        (results/"details.json").write_text(json.dumps({"query_id":qid,"metric":"SoPR","judge_model":model,
            "judge_called":requires_judge,"upstream_labels":labels,"reward":reward},indent=2)+"\n")
        (results/"reward.txt").write_text(str(reward)+"\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace", type=Path, default=Path("/stb-trace/answer.json"))
    parser.add_argument("--results", type=Path, default=Path("/logs/verifier"))
    args = parser.parse_args()
    # A retried/manual grade must never inherit an earlier reward on error.
    (args.results/"reward.txt").unlink(missing_ok=True)
    grade(args.trace, args.results)
