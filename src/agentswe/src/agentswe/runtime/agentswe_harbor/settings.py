from __future__ import annotations

import json
import os
from pathlib import Path

TASK_DIR = Path(os.environ["AGENTSWE_TASK_DIR"])
SHARED_DIR = Path(os.environ["AGENTSWE_CREATION_SHARED"])
PROVIDER_BROKER_DIR = Path(os.environ["AGENTSWE_PROVIDER_BROKER_DIR"])
STATE_DIR = Path(os.environ.get("AGENTSWE_STATE_DIR", "/var/agentswe"))
CANDIDATE_ENV_PREFIX = Path(os.environ["AGENTSWE_CANDIDATE_ENV_PREFIX"])
CANDIDATE_USER = os.environ.get("AGENTSWE_CANDIDATE_USER", "agentswe-candidate")
BROKER_HOST = "127.0.0.1"
# Upstream topology: Candidate -> evaluator broker -> runtime role broker -> provider;
# Result judge -> judge role broker -> provider. Only the role brokers hold provider keys.
BROKER_PORT = 18080
ROLE_BROKER_PORTS = {"runtime": 18081, "judge": 18082}
# Builder -> builder role broker -> provider (the Builder's Codex holds only a client token).
BUILDER_BROKER_PORT = 18083
SECRETS_DIR = STATE_DIR / "secrets"

# Upstream Harbor timeouts: Candidate agent/verifier 660 s, Eval agent 1800 s.
CANDIDATE_JOB_TIMEOUT = 660.0
VERIFIER_JOB_TIMEOUT = 660.0
EVAL_JOB_TIMEOUT = 1800.0

TASK = json.loads((TASK_DIR / "task.json").read_text(encoding="utf-8"))
PROTOCOL = TASK["protocol"]


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set")
    return value


PROVIDERS: dict[str, dict[str, str]] = {}


def _role_provider(role: str) -> dict[str, str]:
    """Provider endpoint, key, model and effort of a role (AGENTSWE_<ROLE>_*), as upstream Config.role()."""
    prefix = f"AGENTSWE_{role.upper()}_"
    return {
        "base_url": _required(prefix + "BASE_URL").rstrip("/"),
        "api_key": _required(prefix + "API_KEY"),
        "model": _required(prefix + "MODEL"),
        "effort": _required(prefix + "EFFORT"),
        "wire": os.environ.get(prefix + "WIRE") or "responses",
    }


def role_broker_url(role: str) -> str:
    return f"http://{BROKER_HOST}:{ROLE_BROKER_PORTS[role]}/v1"


def evaluator_broker_environment() -> dict[str, str]:
    """Environment of upstream's evaluator broker (agentswe/runners/creation_harbor_v1.py:start_broker)."""
    model = _required("AGENTSWE_RUNTIME_MODEL")
    return {
        "AGENTSWE_RUNTIME_RESPONSES_URL": role_broker_url("runtime") + "/responses",
        "AGENTSWE_RUNTIME_MODEL": model,
        "AGENTSWE_RUNTIME_EFFORT": _required("AGENTSWE_RUNTIME_EFFORT"),
        "AGENTSWE_RUNTIME_MODEL_ALIASES": os.environ.get("AGENTSWE_RUNTIME_MODEL_ALIASES") or model.split("/")[-1],
        "AGENTSWE_BROKER_JUDGE_EFFORT": _required("AGENTSWE_JUDGE_EFFORT"),
        "AGENTSWE_EVALUATOR_JUDGE_ROLE": "off",
    }


def _secret_file(name: str, values: dict[str, str], mode: int) -> Path:
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    SECRETS_DIR.chmod(0o711)
    path = SECRETS_DIR / name
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")
    path.chmod(mode)
    return path


def candidate_credential_file() -> Path:
    """Placeholder credentials mounted into Candidates; the broker holds the real key."""
    return _secret_file(
        "candidate.env",
        {"GATEWAY_API_KEY": "broker-only-placeholder", "SERPER_TOKEN": "search-placeholder"},
        0o644,
    )


def secret_file(name: str, values: dict[str, str]) -> Path:
    return _secret_file(name, values, 0o600)


def apply_process_environment() -> None:
    """Variables upstream adapter.py and runtime_contract.py read from the controller environment."""
    for role in ROLE_BROKER_PORTS:
        PROVIDERS[role] = _role_provider(role)
    if os.environ.get("AGENTSWE_BUILDER_API_KEY", "").strip():
        PROVIDERS["builder"] = _role_provider("builder")
    # Provider keys stay with the role brokers, not in the environment of evaluator processes.
    for role in (*ROLE_BROKER_PORTS, "builder"):
        os.environ.pop(f"AGENTSWE_{role.upper()}_API_KEY", None)
    os.environ.update({
        "AGENTSWE_HOME": str(STATE_DIR / "home"),
        "AGENTSWE_TRUSTED_ENV_PREFIX": "/opt/agentswe-trusted-runtime",
        "AGENTSWE_BROKER_HOST": BROKER_HOST,
        "AGENTSWE_CANDIDATE_BROKER_PORT": str(BROKER_PORT),
        # The Result judge reaches the provider only through the judge role broker.
        "AGENTSWE_JUDGE_BASE_URL": role_broker_url("judge"),
        "AGENTSWE_JUDGE_RESPONSES_URL": role_broker_url("judge") + "/responses",
        "AGENTSWE_DEV_CASES": ",".join(PROTOCOL["dev_cases"]),
        "AGENTSWE_HIDDEN_CASES": ",".join(PROTOCOL["heldout_cases"]),
        "AGENTSWE_PROMPT_STRATEGY": "none",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    os.environ.pop("AGENTSWE_CREATE_NETWORK_POOL", None)
    (STATE_DIR / "home").mkdir(parents=True, exist_ok=True)
