# Browser verifier sidecar: the upstream evaluation-codex image with the
# runtime files from the pinned upstream runner commit, mounted at the same
# paths that runtime/eval_container.sh uses.
FROM {verifier_image}

COPY runtime/browser.py /evaluation/run-workflows.py
COPY runtime/parse_results.py /evaluation/parse_results.py
COPY runtime/harnesses/evaluation/ /evaluation/harnesses/
COPY sweeper/verifier_server.py /opt/sweeper/verifier_server.py

WORKDIR /evaluation
CMD ["python3", "-u", "/opt/sweeper/verifier_server.py"]
