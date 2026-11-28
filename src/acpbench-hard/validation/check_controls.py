import json
from pathlib import Path
from evaluate_gen import GRAMMAR_FILE, get_grammar_task, get_parsed_answer
from grammar.grammar_parser import ACPGrammarParser
from evaluators import get_evaluator

results = []
for category in ("app", "areach", "just", "land", "nexta", "prog", "reach", "val"):
    candidates = sorted(Path("/dataset").glob(f"acpbench-hard-{category}-*"))
    # Select a non-None label so a syntactically valid wrong answer is unambiguous.
    task = next(
        t
        for t in candidates
        if (t / "solution/answer.txt").read_text().strip() != "None"
    )
    record = json.loads((task / "tests/record.json").read_text())
    name = f"acp_{category}_gen"
    parser = ACPGrammarParser(GRAMMAR_FILE, get_grammar_task(name))
    wrong = {
        "app": "(nonexistent object)",
        "areach": "None",
        "just": "(nonexistent object)",
        "land": "None",
        "nexta": "(nonexistent object)",
        "prog": "[(nonexistent object)] [(nonexistent object)]",
        "reach": "None",
        "val": "99999",
    }[category]
    for label, answer in [
        ("reference", (task / "solution/answer.txt").read_text()),
        ("wrong", wrong),
        ("empty", ""),
    ]:
        parsed = get_parsed_answer(answer, name, parser)
        reward = 0.0
        if parsed is not None and all(item is not None for item in parsed):
            reward = get_evaluator(record["group"]).get_score([parsed], record)
        expected = float(label == "reference")
        result = dict(
            category=category,
            task=task.name,
            case=label,
            reward=float(reward),
            expected=expected,
        )
        results.append(result)
        assert reward == expected, result
Path("/results/acp-controls.json").write_text(json.dumps(results, indent=2) + "\n")
print("Passed", len(results), "controls")
