#!/bin/bash
# Oracle: apply the record's human-verified gold patch (TimeMachine-bench
# Verified split, field `gold_patch`) to the repository.
set -euo pipefail

patch -p0 -t --no-backup-if-mismatch -d /work < /solution/gold.patch
