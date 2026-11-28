"""Shared verifier egress policy for Harbor and upstream LoopsBench."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import shutil
import tomllib

import yaml


@dataclass(frozen=True)
class VerifierPolicy:
    allowed_hosts: tuple[str, ...]
    upstream_static_tester: bool

    def phase(self) -> dict:
        if self.allowed_hosts:
            return {"network_mode": "allowlist", "allowed_hosts": list(self.allowed_hosts)}
        return {"network_mode": "no-network"}


_CONFIG = tomllib.loads(Path(__file__).with_suffix(".toml").read_text(encoding="utf-8"))


def _load_policies() -> dict[str, VerifierPolicy]:
    policies = {}
    for profile in _CONFIG["profiles"].values():
        hosts = tuple(sorted({
            host for group in profile["host_sets"] for host in _CONFIG["hosts"][group]
        }))
        if any(not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", host) for host in hosts):
            raise ValueError(f"Expected exact hostnames, got {hosts}")
        static_tester = profile.get("upstream_static_tester", False)
        if static_tester and hosts:
            raise ValueError("A loopback-only profile cannot allow external hosts")
        for task_id in profile["task_ids"]:
            if task_id in policies:
                raise ValueError(f"Multiple verifier network profiles for {task_id}")
            policies[task_id] = VerifierPolicy(hosts, static_tester)
    return policies


_POLICIES = _load_policies()


# The released graphics-raster-2d task omits Compose. Use the upstream
# tasks/_template service layout; the regression harness adds the test mounts.
_DEFAULT_UPSTREAM_COMPOSE = """\
services:
  client:
    image: ${LOOPSBENCH_TASK_DOCKER_CLIENT_IMAGE_NAME}
    container_name: ${LOOPSBENCH_TASK_DOCKER_CLIENT_CONTAINER_NAME}
    command: [sh, -c, sleep infinity]
    environment:
      - TEST_DIR=${LOOPSBENCH_TEST_DIR}
    volumes:
      - ${LOOPSBENCH_TASK_LOGS_PATH}:${LOOPSBENCH_CONTAINER_LOGS_PATH}
      - ${LOOPSBENCH_TASK_AGENT_LOGS_PATH}:${LOOPSBENCH_CONTAINER_AGENT_LOGS_PATH}
      - loopsbench_shared:/var/lib/loopsbench/.mirror
  tester:
    image: ${LOOPSBENCH_TASK_DOCKER_CLIENT_IMAGE_NAME}
    container_name: ${LOOPSBENCH_TASK_DOCKER_TESTER_CONTAINER_NAME}
    command: [sh, -c, sleep infinity]
    depends_on: [client]
    volumes:
      - loopsbench_shared:/shared
volumes:
  loopsbench_shared: {}
"""

_MANAGED_LOG_VOLUMES = {
    "${LOOPSBENCH_TASK_LOGS_PATH:-./logs}:${LOOPSBENCH_CONTAINER_LOGS_PATH:-/logs}":
        "${LOOPSBENCH_TASK_LOGS_PATH}:${LOOPSBENCH_CONTAINER_LOGS_PATH}",
    "${LOOPSBENCH_TASK_AGENT_LOGS_PATH:-./agent_logs}:${LOOPSBENCH_CONTAINER_AGENT_LOGS_PATH:-/agent_logs}":
        "${LOOPSBENCH_TASK_AGENT_LOGS_PATH}:${LOOPSBENCH_CONTAINER_AGENT_LOGS_PATH}",
}


def _policy(upstream_id: str) -> VerifierPolicy:
    try:
        return _POLICIES[upstream_id]
    except KeyError:
        raise ValueError(f"No reviewed verifier network policy for {upstream_id!r}") from None


def harbor_network_values(upstream_id: str) -> dict[str, str]:
    """Return the replacements for the network fields in task-template/task.toml."""
    fields = _policy(upstream_id).phase()
    return {"verifier_network": "\n".join(f"{key} = {json.dumps(value)}" for key, value in fields.items())}


def bake_verifier_dependencies(upstream_id: str, dockerfile: str) -> str:
    """Move the reviewed, pinned dependencies into the generated task image."""
    _policy(upstream_id)
    step = _CONFIG["build"].get(upstream_id)
    if step is None:
        return dockerfile
    replacement = step["replacement"].rstrip("\n")
    if len(re.findall(rf"^{re.escape(replacement)}$", dockerfile, re.MULTILINE)) == 1:
        return dockerfile
    anchor = re.compile(rf"^{re.escape(step['anchor'])}$", re.MULTILINE)
    if len(anchor.findall(dockerfile)) != 1:
        raise ValueError(f"{upstream_id}: expected one Dockerfile line {step['anchor']!r}")
    return anchor.sub(lambda _: replacement, dockerfile, count=1)


def prepare_upstream(source_task: Path, harbor_task: Path, output_dir: Path, *,
                     model_host: str | None = None) -> Path:
    """Stage one original task and its generated Harbor build context for comparison."""
    upstream_id = source_task.name
    policy = _policy(upstream_id)
    source_task = source_task.resolve()
    harbor_task = harbor_task.resolve()
    output_dir = output_dir.resolve()
    if output_dir.is_relative_to(source_task) or output_dir.is_relative_to(harbor_task):
        raise ValueError("Place the new output directory outside both input task directories")
    if not (source_task / "task.yaml").is_file():
        raise ValueError(f"Missing upstream task: {source_task}")

    generated = tomllib.loads((harbor_task / "task.toml").read_text(encoding="utf-8"))
    if generated.get("metadata", {}).get("upstream_task_id") != upstream_id:
        raise ValueError(f"The generated Harbor task must have upstream_task_id = {upstream_id!r}")
    phase = policy.phase()
    if any(generated.get("verifier", {}).get(key) != value for key, value in phase.items()):
        raise ValueError(f"{harbor_task}: regenerate the task with its reviewed verifier network policy")
    resources = generated.get("environment", {})
    cpus = resources.get("cpus")
    memory_mb = resources.get("memory_mb")
    if type(cpus) is not int or cpus <= 0 or type(memory_mb) is not int or memory_mb <= 0:
        raise ValueError(f"{harbor_task}: expected positive integer environment.cpus and memory_mb")
    mem_limit = f"{memory_mb}m"

    environment = harbor_task / "environment"
    if not (environment / "Dockerfile").is_file():
        raise ValueError(f"Missing generated Harbor build context: {environment}")
    context_name = ".harbor-environment"
    source_context = source_task / context_name
    if source_context.exists() or source_context.is_symlink():
        raise ValueError(f"{source_task}: {context_name} already exists")

    compose_name = "docker-compose.yaml"
    compose_path = source_task / compose_name
    compose = yaml.safe_load(
        compose_path.read_text(encoding="utf-8")
        if compose_path.is_file() else _DEFAULT_UPSTREAM_COMPOSE
    )
    if not isinstance(compose, dict) or not isinstance(compose.get("services"), dict):
        raise ValueError(f"{source_task}: expected upstream Compose services")
    client = compose["services"].get("client")
    tester = compose["services"].get("tester")
    if not isinstance(client, dict) or not isinstance(tester, dict):
        raise ValueError(f"{source_task}: expected upstream client and tester services")
    client["build"] = {"context": f"./{context_name}", "dockerfile": "Dockerfile"}
    if "volumes" in client:
        # Upstream always supplies these managed paths during execution. Only
        # replace the exact released expressions its validator does not accept.
        client["volumes"] = [
            _MANAGED_LOG_VOLUMES.get(volume, volume) if isinstance(volume, str) else volume
            for volume in client["volumes"]
        ]
    for service in (client, tester):
        service.update(cpus=cpus, mem_limit=mem_limit)
        # Compose requires direct and deploy limits to agree if both are set.
        deployment_resources = (service.get("deploy") or {}).get("resources") or {}
        limits = deployment_resources.get("limits") or {}
        if "cpus" in limits:
            limits["cpus"] = str(cpus)
        if "memory" in limits:
            limits["memory"] = mem_limit

    # Upstream rejects host networking and its sidecar cannot share the
    # NET_ADMIN client's namespace. The oracle comparison isolates these
    # clients statically and retains NET_ADMIN for TCP's loopback aliases.
    if model_host and client.get("network_mode") == "host":
        # Harbor omits host networking. Let the native harness attach its
        # managed model-endpoint network for the paired Codex solve phase.
        client.pop("network_mode")
    static_client = (
        client.get("network_mode") == "host" or "NET_ADMIN" in client.get("cap_add", [])
    )
    if static_client and model_host:
        raise ValueError(f"{upstream_id}: isolated client cannot reach the parity model")
    if static_client:
        client.pop("networks", None)
        client["network_mode"] = "none"
    if policy.upstream_static_tester:
        if "NET_ADMIN" not in tester.get("cap_add", []):
            raise ValueError(f"{upstream_id}: the upstream tester must retain NET_ADMIN")
        tester.pop("networks", None)
        tester["network_mode"] = "none"

    # The upstream validator requires a relative, task-local build context.
    # Copy the Dockerfile, ignore rules and build inputs without rewriting them.
    dataset = output_dir / "tasks"
    staged_task = dataset / upstream_id
    config = {
        "dataset_path": str(dataset),
        "output_path": str(output_dir / "runs"),
        "task_ids": [upstream_id],
        "agent": "oracle",
        "docker_image_strategy": "local-build",
        "network": {
            "environment": {"network_mode": "public"},
            # Both phase and baseline must be public to skip the upstream
            # controller. These services have no egress in Compose.
            "agent": {"network_mode": "public" if static_client else "no-network"},
            "verifier": {"network_mode": "public"} if policy.upstream_static_tester else phase,
        },
    }
    comments = ["# Oracle-only verifier comparison using the generated Harbor build inputs."]
    if static_client:
        comments.append("# Compose isolates the client with network_mode: none because it cannot use the upstream sidecar.")
    if policy.upstream_static_tester:
        comments.append("# Compose isolates the TCP tester with network_mode: none and retains NET_ADMIN.")
    if model_host:
        config["network"]["agent"] = {"network_mode": "allowlist", "allowed_hosts": [model_host]}
        comments = ["# Staged upstream parity comparison."]
    rendered_config = "\n".join(comments) + "\n" + yaml.safe_dump(config, sort_keys=False)

    output_dir.mkdir()
    try:
        shutil.copytree(source_task, staged_task, symlinks=True)
        shutil.copytree(environment, staged_task / context_name, symlinks=True)
        staged_compose = staged_task / compose_name
        staged_compose.unlink(missing_ok=True)
        staged_compose.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")
        run_config = output_dir / "run.yaml"
        run_config.write_text(rendered_config, encoding="utf-8")
    except BaseException:
        shutil.rmtree(output_dir)
        raise
    return run_config


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare-upstream", help="stage a single upstream verifier comparison")
    prepare.add_argument("--source-task", required=True, type=Path)
    prepare.add_argument("--harbor-task", required=True, type=Path)
    prepare.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()

    try:
        config_path = prepare_upstream(args.source_task, args.harbor_task, args.output_dir)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        parser.error(str(exc))
    print(config_path)


if __name__ == "__main__":
    main()
