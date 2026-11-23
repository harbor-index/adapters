
# --- JavaScript: Node.js v16.14.0 + js-md5 0.7.3 (the versions of the reference setup) ---
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) node_arch=x64; sha256=2c69e7b040c208b61ebf9735c63d2e5bcabfed32ef05a9b8dd5823489ea50d6b ;; \
      arm64) node_arch=arm64; sha256=82d71968c82eb391f463df62ba277563a3bd01ce43bba0e7e1c533991567b8fe ;; \
      *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/node.tar.gz \
      "https://nodejs.org/dist/v16.14.0/node-v16.14.0-linux-${node_arch}.tar.gz"; \
    echo "${sha256}  /tmp/node.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/node.tar.gz -C /usr/local --strip-components=1 --no-same-owner \
      --exclude='*.md' --exclude=LICENSE; \
    rm /tmp/node.tar.gz; \
    npm install -g js-md5@0.7.3; \
    npm cache clean --force; \
    rm -rf /root/.npm; \
    node --version
ENV NODE_PATH=/usr/local/lib/node_modules
