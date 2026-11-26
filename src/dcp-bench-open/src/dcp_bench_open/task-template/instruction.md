Implement the following constraint modeling problem using Python and CPMpy.

{brief}

Write one instance-agnostic `/workspace/model.py` defining `build(instance)`.
Return `(model, outputs)` where `model` is a CPMpy Model and `outputs` contains
exactly these declared names: {outputs}. The runner handles solving and output
serialization; do not call `solve()` or print a solution in your file.

Read `/workspace/CPMPY_SKILL.md` for the native solver integration contract.
The public embedded input is `/workspace/example.json`. Its declared fields are {input_fields}.
The upstream input declarations and their field annotations are in `/workspace/INPUT.md`.
Read problem data from `instance` so the same file works on additional instances with the same fields.
A task passes only when the native v0.3.0 evaluator accepts every selected
released instance. Candidate execution and reference operations each have a
60-second limit per instance, with one candidate solution checked per instance.
Only your single `model.py` file is passed to the native solver sandbox.
