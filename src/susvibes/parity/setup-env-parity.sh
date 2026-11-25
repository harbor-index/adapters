#!/bin/bash
# Parity variant of evaluation_harness/claude_code/setup-env.sh (LeiLiLab/susvibes@5d957e6).
# Passed to the original runner with --setup_script; the runner itself is unchanged.
# Differences from the original script:
#   - Claude Code is installed with the same command Harbor's claude-code agent
#     uses (the release bootstrap script) at the version pinned for parity.
#     The original installs @anthropic-ai/claude-code@1.0.128 with npm.
#   - /root/.claude_env does not hard-code a model (the runner exports ANTHROPIC_MODEL)
#     and carries the variables that Harbor's claude-code agent also sets.

echo "Installing Claude Code 2.1.330..."

# Harbor's agent installs these system packages when their command is missing
# (curl, bash, nodejs, npm, procps); do the same here.
missing=""
command -v curl >/dev/null 2>&1 || missing="$missing curl"
command -v bash >/dev/null 2>&1 || missing="$missing bash"
command -v node >/dev/null 2>&1 || missing="$missing nodejs"
command -v npm >/dev/null 2>&1 || missing="$missing npm"
command -v ps >/dev/null 2>&1 || missing="$missing procps"
if [ -n "$missing" ]; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update && apt-get install -y $missing
fi

curl -fsSL https://downloads.claude.ai/claude-code-releases/bootstrap.sh | bash -s -- 2.1.330

echo "Setting up environment..."
cat > /root/.claude_env << 'EOF'
export PATH="$HOME/.local/bin:$PATH"
export FORCE_AUTO_BACKGROUND_TASKS="1"
export ENABLE_BACKGROUND_TASKS="1"
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1"
export IS_SANDBOX="1"
export ANTHROPIC_DEFAULT_SONNET_MODEL="claude-haiku-4-5-20251001"
export ANTHROPIC_DEFAULT_OPUS_MODEL="claude-haiku-4-5-20251001"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="claude-haiku-4-5-20251001"
export ANTHROPIC_DEFAULT_FABLE_MODEL="claude-haiku-4-5-20251001"
export CLAUDE_CODE_SUBAGENT_MODEL="claude-haiku-4-5-20251001"
EOF

echo "source /root/.claude_env" >> /root/.bashrc

echo "Testing installation..."
source /root/.claude_env

if command -v claude >/dev/null 2>&1; then
  claude --version
  echo "INSTALL_SUCCESS"
else
  echo "Installation failed" >&2
  exit 1
fi
