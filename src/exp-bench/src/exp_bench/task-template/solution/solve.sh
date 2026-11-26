#!/bin/bash
# EXP-Bench reference solution (oracle).
# EXP-Bench has no runnable reference trajectory, so the oracle restores the masked
# source files from the pinned upstream commit and writes the ground-truth design and
# expected outcome as its answer. It validates the environment, masking, patch and
# judge plumbing; run it with EXP_BENCH_ORACLE_VALIDATION=1 (see README).
set -euo pipefail

cd /workspace
if [ -d /solution/oracle_files ]; then
    cp -a /solution/oracle_files/. /workspace/
fi
cp /solution/oracle_answer.json "/workspace/{output_filename}"

cat > /workspace/reproduce_exp_bench.sh <<'EOF'
#!/bin/bash
# Oracle placeholder: the reference answer was not produced by running the experiment.
echo "EXP-Bench oracle: reference source files restored; experiment not executed."
EOF
chmod +x /workspace/reproduce_exp_bench.sh
echo "EXP-Bench oracle solution written."
