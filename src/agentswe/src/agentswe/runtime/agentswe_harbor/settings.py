from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

# Image layout (the task's evaluator and verifier Dockerfiles).
TASK_DIR = Path(os.environ["AGENTSWE_TASK_DIR"])
SHARED_DIR = Path(os.environ["AGENTSWE_CREATION_SHARED"])
BROKER_DIR = Path(os.environ["AGENTSWE_PROVIDER_BROKER_DIR"])
CANDIDATE_ENV_PREFIX = Path(os.environ["AGENTSWE_CANDIDATE_ENV_PREFIX"])
TRUSTED_ENV_PREFIX = Path("/opt/agentswe-trusted-runtime")
CANDIDATE_USER = "agentswe-candidate"

# Shared by the evaluator and its worker. RUN_DIR is upstream one_stop's run directory.
STATE_DIR = Path("/var/agentswe")
RUN_DIR = STATE_DIR / "run"
JOBS_DIR = RUN_DIR / "jobs"
SECRETS_DIR = STATE_DIR / "secrets"
WORKER_DIR = STATE_DIR / "worker"
# Provider keys never leave the primary container.
KEYS_DIR = Path("/run/agentswe-keys")

# Upstream topology: Candidate -> evaluator broker -> runtime role broker -> provider;
# Result judge -> judge role broker -> provider; Builder -> builder role broker -> provider.
BROKER_HOST = "127.0.0.1"
EVALUATOR_BROKER_PORT = 18080
ROLE_BROKER_PORTS = {"runtime": 18081, "judge": 18082}
BUILDER_BROKER_PORT = 18083

TASK = json.loads((TASK_DIR / "task.json").read_text(encoding="utf-8"))
PROTOCOL = TASK["protocol"]
N_CONCURRENT = int(TASK["runner_config"]["n_concurrent"])


def role_url(role: str) -> str:
    return f"http://{BROKER_HOST}:{ROLE_BROKER_PORTS[role]}/v1"


def provider(role: str) -> dict[str, str]:
    """A role's provider endpoint, key, model and effort (AGENTSWE_<ROLE>_*), as upstream Config.role()."""
    prefix = f"AGENTSWE_{role.upper()}_"
    values = {name: os.environ.get(prefix + name.upper(), "").strip() for name in ("base_url", "api_key", "model", "effort")}
    missing = [prefix + name.upper() for name, value in values.items() if not value]
    if missing:
        raise RuntimeError(f"not set: {', '.join(missing)}")
    return {**values, "base_url": values["base_url"].rstrip("/"), "wire": os.environ.get(prefix + "WIRE") or "responses"}


def prepare() -> None:
    """The environment upstream's runner gives one_stop.py (agentswe/runners/creation_harbor_v1.py), and state
    directories a Candidate, which runs unprivileged, cannot read."""
    os.environ.update({
        "AGENTSWE_HOME": str(STATE_DIR / "home"),
        "AGENTSWE_TRUSTED_ENV_PREFIX": str(TRUSTED_ENV_PREFIX),
        "AGENTSWE_CANDIDATE_CREDENTIAL_FILE": str(SECRETS_DIR / "candidate.env"),
        "AGENTSWE_BROKER_HOST": BROKER_HOST,
        "AGENTSWE_CANDIDATE_BROKER_PORT": str(EVALUATOR_BROKER_PORT),
        "AGENTSWE_JUDGE_BASE_URL": role_url("judge"),
        "AGENTSWE_JUDGE_RESPONSES_URL": role_url("judge") + "/responses",
        "AGENTSWE_DEV_CASES": ",".join(PROTOCOL["dev_cases"]),
        "AGENTSWE_HIDDEN_CASES": ",".join(PROTOCOL["heldout_cases"]),
        "AGENTSWE_PROMPT_STRATEGY": "none",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.chmod(0o711)
    for directory in (RUN_DIR, SECRETS_DIR, WORKER_DIR, STATE_DIR / "home"):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)


def import_upstream():
    """The task's upstream adapter.py and one_stop.py, imported as one_stop.py imports them."""
    for path in (str(SHARED_DIR), str(TASK_DIR / "adapter")):
        if path not in sys.path:
            sys.path.insert(0, path)
    return importlib.import_module("adapter"), importlib.import_module("one_stop")
