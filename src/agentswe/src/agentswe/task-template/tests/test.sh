#!/bin/bash
# Held-out evaluation of the frozen delivery with upstream's Candidate and Eval jobs.
# Writes /logs/verifier/reward.txt (hidden mean / 100); an infrastructure failure exits
# non-zero without a reward instead of recording a zero. Upstream's per-case jobs reuse
# /logs/verifier, so this trial's own log is written elsewhere and copied back at the end.
python3 -m agentswe_harbor.verify > /var/agentswe/verify.log 2>&1
status=$?
cp /var/agentswe/verify.log /logs/verifier/verify.log
exit $status
