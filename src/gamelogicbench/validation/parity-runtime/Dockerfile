FROM gamelogicbench-runtime:godot-4.4
COPY codex /usr/local/bin/codex
COPY rg /usr/local/bin/rg
RUN test "$(codex --version)" = 'codex-cli 0.144.1'
WORKDIR /workspace/game
