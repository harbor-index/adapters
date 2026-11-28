# Preload locked Rust dependencies while building, for the offline evaluator.
RUN export PATH="/root/.cargo/bin:$PATH"; \
    test -s Cargo.lock && \
    CARGO_NET_OFFLINE=false cargo fetch --locked --target "$(rustc -vV | sed -n 's/^host: //p')"
