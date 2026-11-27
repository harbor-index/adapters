#!/usr/bin/env bash
# Run the setup part of the product image's own entrypoint.
# Upstream starts every product container with the image entrypoint
# (product-container-entrypoint), and some images use it to prepare the checkout
# (e.g. link prebuilt node_modules, copy dist) before `product-yarn start`.
# The trailing `exec sleep infinity` only keeps the container alive, so drop it.
ep=/usr/local/bin/product-container-entrypoint
[ -f "$ep" ] || exit 0
grep -vE '^[[:space:]]*exec[[:space:]]+sleep([[:space:]]|$)' "$ep" > /tmp/sweeper-image-setup.sh
timeout 600 bash /tmp/sweeper-image-setup.sh
