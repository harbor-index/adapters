# Acceptance status

This adapter remains **Draft**. The Oracle baseline and recorded model scores are
retained as experiment observations, not evidence of merge readiness. The review
uses adapter contract v1 at `01c00a6bdf7f2c6d1a1aaeb85c1197db62f0423c`.

## Grading integrity — reproduced blocker

A private negative test submitted an inert navigation controller that did not
solve the task. The adapted evaluator accepted its forged intermediate verdict;
the pinned original harness also accepted the submission in all 20 cells.
No model API was involved. The reproducer and raw evidence are retained privately.

The frozen judge and submitted GDScript execute within the same Godot process and
share access to the authoritative result channel. Worker UIDs isolate cells from
each other and protect the supervisor's final reward file, but do not authenticate
the intermediate verdict against code running in that worker. Frozen files at
launch and a consistent exit code are insufficient.

Changing file permissions, requiring more JSON fields, or adding a secret inside
the same process would not resolve this trust boundary. A complete correction
requires a restricted controller execution boundary or an external authoritative
judge with a task-specific observation/action protocol. Any such change must be
applied consistently to the original baseline and adapter, followed by positive,
negative and adversarial calibration before new parity runs. Upstream sharing the
defect does not waive Harbor's grading-integrity requirement.

The historical Oracle 72/72 result establishes reference behavior only. A scan of
changed GDScript files in all 144 scored submissions found no result-channel or
process-control indicators in successful submissions; two `quit()` calls were in
a diagnostic script of a failed submission. This text scan is not proof that all
submission behavior was benign, and it does not repair the evaluator.

## Parity configuration — provisional cohort

The recorded scores remain 13.8889 ± 2.7778% (original) and 18.0556 ± 3.6747%
(Harbor). Their ranges overlap, but the Harbor cohort used two driver revisions:

| Harbor attempts retained in final scores | Prompt delivery | Count |
| --- | --- | ---: |
| Round 1 excluding repaired `atom_target_selection`; round 2 | argv | 47 |
| Repaired `atom_target_selection`; round 3 | file/stdin | 25 |

This mapping is reconstructed from the launch/recovery records, not a per-attempt
driver hash captured at execution. It is not a single frozen driver cohort.
The stdin patch and its real-command-builder check remain available in this
directory. That check proves prompt bytes are preserved and the launch command
omits them; it does not turn historical mixed settings into a controlled run.

Preserve all old attempts. Freeze the corrected judge, Harbor driver, executable,
image digest, model endpoint/ID, inputs, budgets and retry policy **before** any new
experiment. Apply the same relevant changes on both sides, then follow the guide's
5–10-task sanity → one paired full run → three paired runs order. The previous
pilot had four tasks and rounds 1/2 overlapped, so the historical ramp-up also
differs from the current guide. Do not combine repaired and old cohort scores.
The provider exposed `gpt-5-mini` without a dated snapshot; backend version
stability remains unverified and must be disclosed rather than inventing an ID.

All seven disagreement pairs (14 saved submissions) were replayed through the
opposite evaluator: **294/294 cell verdicts agreed**, with zero model API calls.
See [cross-grade evidence](crossgrade-disagreements.json). This supports code-output
differences for those observed score disagreements, but does not prove that
sampling alone caused them or that other adaptation effects are absent. It cannot
replace frozen-configuration parity or adversarial integrity checks.

## Redistribution and publication — not cleared

Neither pinned repository has a root LICENSE/COPYING/NOTICE file; GitHub also
reports no detected repository license. The task-library README's statement about
vendored project licenses is not a blanket license for benchmark-authored briefs,
judges, modifications or solutions. In particular, no tracked license notice was
found under `repo_damage_control`, `repo_dothop_move_engine`, `repo_td_income` or
`repo_watch_rotation`. YouTD 2 documentation distinguishes MIT source code from
CC-BY-NC 4.0 assets. These observations are an inventory, not a conclusion that
redistribution is necessarily forbidden.

Public dataset release needs a license or explicit permission covering the exact
benchmark materials and intended Harbor redistribution, plus third-party notice
and asset review. Generated tasks and exploit details remain private. A complete
72-task manifest and a sanitized evidence export are staged locally; neither is
published or a substitute for the required dataset/Hugging Face PRs. No Hugging
Face authentication was available for the upload workflow during this audit.

Required external steps remain: establish redistribution rights; resolve the
judge boundary with the benchmark/Harbor maintainers; submit the corrected
dataset and sufficient reviewed evidence; obtain real PR links; verify registry
Oracle after publication; then request formal review. Do not mark this PR Ready
for Review while these requirements are unresolved.
