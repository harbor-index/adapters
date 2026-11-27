#!/usr/bin/env bash
# Start the product the way the upstream runtime/product.sh does: the app
# listens on container port 3000 and is reachable at 127.0.0.1:13200. Upstream
# publishes 13200 with Docker; here a local TCP forwarder plays that role.
# Readiness is checked by the [environment.healthcheck] in task.toml.
if [ "${SWEEPER_AUTOSTART:-1}" = "1" ]; then
  bash /opt/sweeper/image-setup.sh >/tmp/sweeper-image-setup.log 2>&1 || true
  socat TCP-LISTEN:13200,bind=127.0.0.1,fork,reuseaddr TCP:127.0.0.1:3000 \
    >/tmp/sweeper-forward.log 2>&1 &
  setsid bash -lc 'cd /workspace/product && HOST=0.0.0.0 PORT=3000 BROWSER=none product-yarn start >/tmp/product-dev-server.log 2>&1' \
    </dev/null >/dev/null 2>&1 &

  # Some product start scripts append /etc/hosts lines that map GitHub and
  # similar hosts to 0.0.0.0 so the upstream agent cannot fetch known fixes.
  # Upstream runs the agent in a separate container behind an egress
  # allowlist; here the agent shares this container and Harbor's agent-phase
  # allowlist blocks those hosts instead. Once the product has started, drop
  # just these tooling hosts so Harbor can install the agent; every other
  # mapping the product added stays.
  (
    for _ in $(seq 1 900); do
      code=$(curl --noproxy '*' -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:13200 || true)
      [ -n "$code" ] && [ "$code" != 000 ] && [ "$code" -lt 500 ] && break
      sleep 1
    done
    sleep 2
    awk 'BEGIN { split("github.com api.github.com raw.githubusercontent.com objects.githubusercontent.com codeload.github.com nodejs.org registry.npmjs.org pypi.org files.pythonhosted.org", t, " "); for (i in t) tool[t[i]] = 1 }
      {
        line = $0; sub(/#.*/, "", line); n = split(line, f, /[ \t]+/); start = (f[1] == "") ? 2 : 1
        if ((f[start] == "0.0.0.0" || f[start] == "127.0.0.1" || f[start] == "::1") && n > start) {
          hit = 0; keep = ""
          for (i = start + 1; i <= n; i++) { if (f[i] == "") continue; if (f[i] in tool) hit = 1; else keep = keep " " f[i] }
          if (hit) { if (keep != "") print f[start] keep; next }
        }
        print $0
      }' /etc/hosts > /tmp/sweeper-hosts && cat /tmp/sweeper-hosts > /etc/hosts
  ) >/dev/null 2>&1 &
fi
exec "$@"
