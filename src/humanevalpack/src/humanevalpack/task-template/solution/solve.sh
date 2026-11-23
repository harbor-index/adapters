#!/bin/bash
# Oracle for a HumanEvalPack task.
#
# {solution_file} next to this script is the benchmark's reference solution:
# the dataset fields `prompt` + `canonical_solution`, which is what the original
# harness evaluates with --check_references (get_reference(doc, get_solution=True)).
set -euo pipefail

mkdir -p {solution_dir}
cp "$(dirname "${BASH_SOURCE[0]}")/{solution_file}" {solution_dir}/{solution_file}
echo "Reference solution written to {solution_dir}/{solution_file}"
