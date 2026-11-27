#!/bin/bash
set -euo pipefail

# Oracle: write the reference answer letter from the MedXpertQA label.
echo "{{ANSWER}}" > /app/answer.txt
