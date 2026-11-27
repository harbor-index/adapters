#!/usr/bin/env bash
# Install the Playwright CLI and its Chromium for the agent, then prove that
# Chromium starts. `playwright install --with-deps` only knows recent distro
# package names; older product bases (e.g. Debian 10) fall back to installing
# the usual Chromium runtime libraries one by one under their generic names.
set -euo pipefail

NODE_BIN=/opt/sweeper-agent-node/bin
export PATH="$NODE_BIN:$PATH"
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/ms-playwright}"

npm install -g --no-audit --no-fund "playwright@$1"

if ! playwright install --with-deps chromium; then
  echo "playwright --with-deps failed on this base; installing runtime libraries individually" >&2
  if ! playwright install chromium; then
    # Playwright refuses distros it does not list (e.g. "does not support
    # chromium on debian11-x64"). The Linux x64 Chromium build is the same
    # binary; download the Ubuntu 22.04 one and let the smoke test decide.
    echo "retrying with PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu22.04-x64" >&2
    PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu22.04-x64 playwright install chromium
  fi
  apt-get update
  for pkg in libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libdbus-1-3 \
             libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
             libcairo2 libasound2 libatspi2.0-0 libx11-6 libx11-xcb1 libxcb1 libxext6 libxshmfence1 \
             libglib2.0-0 libexpat1 libfontconfig1 libfreetype6 fonts-liberation fonts-noto-color-emoji; do
    # Newer Debian/Ubuntu renamed several of these with a t64 suffix (64-bit time_t).
    apt-get install -y --no-install-recommends "$pkg" >/dev/null 2>&1 \
      || apt-get install -y --no-install-recommends "${pkg}t64" >/dev/null 2>&1 \
      || echo "skip $pkg" >&2
  done
fi

cat > /usr/local/bin/playwright <<'EOF'
#!/usr/bin/env bash
exec /opt/sweeper-agent-node/bin/node /opt/sweeper-agent-node/lib/node_modules/playwright/cli.js "$@"
EOF
chmod 0755 /usr/local/bin/playwright
if ! command -v node >/dev/null 2>&1; then
  ln -s "$NODE_BIN/node" /usr/local/bin/node
fi

# Smoke test: Chromium must launch headless and render a page.
NODE_PATH=/opt/sweeper-agent-node/lib/node_modules "$NODE_BIN/node" -e '
const { chromium } = require("playwright");
(async () => {
  const b = await chromium.launch({ args: ["--no-sandbox"] });
  const p = await b.newPage();
  await p.setContent("<title>ok</title>");
  console.log("chromium ok:", await p.title(), b.version());
  await b.close();
})().catch((e) => { console.error(e); process.exit(1); });
'
rm -rf /var/lib/apt/lists/* /root/.npm
