"""Check every generated answer against the untouched upstream evaluator."""

import json
from pathlib import Path
from evaluate_gen import GRAMMAR_FILE, get_grammar_task, get_parsed_answer
from grammar.grammar_parser import ACPGrammarParser
from evaluators import get_evaluator

results = []
for task in sorted(Path("/dataset").iterdir()):
    record = json.loads((task / "tests/record.json").read_text())
    category = (task / "tests/category.txt").read_text()
    name = f"acp_{category}_gen"
    parser = ACPGrammarParser(GRAMMAR_FILE, get_grammar_task(name))
    parsed = get_parsed_answer((task / "solution/answer.txt").read_text(), name, parser)
    try:
        score = get_evaluator(record["group"]).get_score([parsed], record)
        result = {"task": task.name, "reward": float(score)}
    except Exception as error:
        result = {"task": task.name, "error": repr(error)}
    results.append(result)
    if result.get("reward") != 1:
        print(result, flush=True)
    if len(results) % 130 == 0:
        print("Completed", len(results), flush=True)
Path("/results/acp-reference-check.json").write_text(
    json.dumps(results, indent=2) + "\n"
)
print(
    "Passed", sum(r.get("reward") == 1 for r in results), "/", len(results), flush=True
)
