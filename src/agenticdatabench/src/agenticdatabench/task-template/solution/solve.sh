#!/bin/bash
# Oracle: copy upstream's gold files into /workspace under the output names the
# task asks for. For chart tasks this includes the plot data (.json, .npy) that
# image.py's Plotprocess.plot_process writes next to the chart.
set -euo pipefail

if [ -z "$(ls -A /solution/output 2>/dev/null)" ]; then
    echo "No reference outputs for this task" >&2
    exit 1
fi
cp -r /solution/output/. /workspace/
