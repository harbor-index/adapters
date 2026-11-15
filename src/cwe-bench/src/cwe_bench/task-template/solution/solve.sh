#!/bin/bash
# Written into a generated task only when the adapter runs with --omit-solutions.
#
# CWE-bench's access conditions ask users not to republish its reference solutions,
# so this copy of the task carries none. To run the oracle, regenerate the dataset
# with the cwe-bench adapter without --omit-solutions (this needs your own access to
# the gated upstream dataset).
echo "cwe-bench: the reference solution is omitted from this copy of the task." >&2
echo "Regenerate with the cwe-bench adapter (without --omit-solutions) to run the oracle." >&2
exit 1
