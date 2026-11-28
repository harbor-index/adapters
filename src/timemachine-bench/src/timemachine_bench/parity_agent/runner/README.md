# Upstream baseline agent (vendored)

Copied from [tohoku-nlp/timemachine-bench](https://github.com/tohoku-nlp/timemachine-bench)
at commit `9928dbf1af1405433d2c2e40227f39fd831d3863`, Apache-2.0
(Copyright the TimeMachine-bench authors). `../agent.py` installs this directory into
the task container and runs `solve_openai.py` there.

| File | Upstream path | Changes |
|------|---------------|---------|
| `solve_openai.py` | `agents/src/main/solve_openai.py` | none |
| `utils/prompt.py` | `agents/src/utils/prompt.py` | none |
| `utils/__init__.py` | `agents/src/utils/__init__.py` | none |
| `utils/tools.py` | `agents/src/utils/tools.py` | `execute_tests` runs `bash /work/<test script>` in place instead of `docker run -v <repo>:/work <image>` (same 600 s limit); `import os` added. Marked `[Harbor port]`. |
| `requirements.txt` | `agents/uv.lock` | exported with `uv export --frozen --no-hashes --no-emit-project` |

The files are kept as upstream wrote them, so they are excluded from this adapter's
lint and format settings.
