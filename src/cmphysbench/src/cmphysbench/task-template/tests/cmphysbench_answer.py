"""Answer extraction and scoring shared by the Harbor verifier and the
original-side parity runner. Both sides must use this exact file.

Follows upstream CMPhysBench evaluation.py (commit b2cd857):
  - extract_boxed_latex: content of the FIRST \\boxed{...} in the response,
    naive brace matching; no box -> empty answer;
  - score_one_safe: empty reference or empty answer -> 0; otherwise
    SEED(reference, answer, answer_type); any exception -> 0.

Reward mapping:
  seed_score in [0, 100]  upstream SEED score
  reward    = seed_score / 100
  accuracy  = 1.0 if |seed_score - 100| < 1e-4 else 0.0   (upstream ACC rule)
"""

from __future__ import annotations


def extract_boxed_latex(text: str) -> str:
    """Verbatim logic of upstream evaluation.extract_boxed_latex."""
    if not isinstance(text, str):
        return ""
    start = text.find(r"\boxed{")
    if start == -1:
        return ""
    start += len(r"\boxed{")
    depth = 1
    end = start
    while end < len(text):
        if text[end] == "{":
            depth += 1
        elif text[end] == "}":
            depth -= 1
            if depth == 0:
                return text[start:end].strip()
        end += 1
    return ""


def score_response(reference: str, response: str, answer_type: str) -> dict:
    """Score one response with upstream SEED (package `SEED` must be importable)."""
    from SEED.SEED import SEED  # upstream package, copied verbatim

    answer = extract_boxed_latex(response)
    if not reference or not answer:
        seed_score = 0.0
    else:
        try:
            seed_score = float(SEED(reference, answer, answer_type)[0])
        except Exception:  # upstream score_one_safe maps exceptions to 0
            seed_score = 0.0
    return {
        "reward": seed_score / 100.0,
        "seed_score": seed_score,
        "accuracy": 1.0 if abs(seed_score - 100.0) < 1e-4 else 0.0,
        "extracted_answer": answer,
    }
