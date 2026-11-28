#!/bin/bash
# Held-out evaluation of the frozen delivery with upstream's Candidate and Eval jobs.
# Writes /logs/verifier/reward.txt (hidden mean / 100); an infrastructure failure exits
# non-zero without a reward instead of recording a zero.
set -euo pipefail
exec python3 -m agentswe_harbor.verify
