"""Answer extraction and scoring shared by the Harbor verifier and the
original-side parity runner. Both sides must use this exact file.

Protocol (PHYBench paper, Appendix D): the final answer is the content of the
\\boxed{} environment in the response; text outside the box is ignored. The
extracted expression is scored with the upstream EED function
(phybench-official/phybench, EED/EED.py), called as
EED(extract(reference), extract(response)).

Reward mapping:
  eed_score in [0, 100]  upstream EED score
  reward    = eed_score / 100
  accuracy  = 1.0 if eed_score == 100 else 0.0
"""

from __future__ import annotations

BOX_COMMANDS = ("\\boxed", "\\fbox")


def _matching_brace_content(text: str, open_index: int) -> str | None:
    """Return the content of the brace group starting at text[open_index] == '{'."""
    depth = 0
    i = open_index
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2  # skip escaped character, e.g. \{ or \}
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[open_index + 1 : i]
        i += 1
    return None


def extract_final_answer(response: str) -> str:
    """Content of the last \\boxed{...} (or \\fbox{...}) in the response.

    If the response has no box, the whole stripped response is used, so a bare
    expression (for example the oracle's reference answer) is scored as is.
    An unterminated box yields an empty answer (score 0).
    """
    text = response.strip()
    last_pos = -1
    last_cmd = ""
    for cmd in BOX_COMMANDS:
        pos = text.rfind(cmd + "{")
        if pos > last_pos:
            last_pos, last_cmd = pos, cmd
    if last_pos < 0:
        return text
    content = _matching_brace_content(text, last_pos + len(last_cmd))
    return content.strip() if content is not None else ""


def score_response(reference: str, response: str) -> dict:
    """Score one response against the reference answer with upstream EED."""
    from EED import EED  # upstream module; its directory must be on sys.path

    # The same extraction is applied to the reference: four reference answers
    # are themselves wrapped in \boxed{}; all others are returned stripped.
    gold = extract_final_answer(reference)
    answer = extract_final_answer(response)
    if answer:
        try:
            eed_score = float(EED(gold, answer)[0])
        except Exception:  # upstream can raise on some malformed expressions
            eed_score = 0.0
    else:
        eed_score = 0.0
    return {
        "reward": eed_score / 100.0,
        "eed_score": eed_score,
        "accuracy": 1.0 if eed_score == 100.0 else 0.0,
        "extracted_answer": answer,
    }
