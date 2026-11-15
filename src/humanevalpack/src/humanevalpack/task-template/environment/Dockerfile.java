
# --- Java: OpenJDK 18 (GA build 18+36, 2022-03-22) ---
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) jdk_arch=x64; sha256=0f60aef7b8504983d6e374fe94d09a7bedcf05ec559e812d801a33bd4ebd23d0 ;; \
      arm64) jdk_arch=aarch64; sha256=dff2860ba24c3f70f32ad3ac9b03f768dd28044bbda87c9607654fd03795c2ab ;; \
      *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/jdk.tar.gz \
      "https://download.java.net/java/GA/jdk18/43f95e8614114aeaa8e8a5fcf20a682d/36/GPL/openjdk-18_linux-${jdk_arch}_bin.tar.gz"; \
    echo "${sha256}  /tmp/jdk.tar.gz" | sha256sum -c -; \
    mkdir -p /opt/toolchain/jdk; \
    tar -xzf /tmp/jdk.tar.gz -C /opt/toolchain/jdk --strip-components=1 --no-same-owner; \
    rm -rf /tmp/jdk.tar.gz /opt/toolchain/jdk/jmods /opt/toolchain/jdk/lib/src.zip; \
    /opt/toolchain/jdk/bin/java -version
ENV JAVA_HOME=/opt/toolchain/jdk \
    PATH=/opt/toolchain/jdk/bin:$PATH
