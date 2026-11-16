# Agent environment: mirrors the upstream MBABench coding-agent sandbox
# (coding-agents/docker/Dockerfile) minus what Harbor provides itself:
#   - the agent CLI (Claude Code / Codex) is installed by Harbor's agent setup,
#     pinned with the agent's `version` kwarg;
#   - egress is restricted by the task's [agent] network allowlist instead of
#     the upstream iptables firewall (iptables / ipset / dnsutils dropped).
# curl is added so Harbor's agent installers need no extra apt step.
FROM node:22-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv \
        ca-certificates curl procps \
        libreoffice-calc \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --break-system-packages --no-cache-dir openpyxl pandas

# The workspace upstream hands the agent: PROMPT.md, HOUSE_STANDARDS.md and
# starting_files/<workbook>.xlsx. The agent writes /workspace/solution.xlsx.
WORKDIR /workspace
COPY workspace/ /workspace/
