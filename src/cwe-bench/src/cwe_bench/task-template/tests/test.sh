#!/bin/bash

# The adapter copies each upstream tests/test.sh and adds a patch-application step.
# Harbor runs that generated script in the separate verifier environment.
# The generated verifier reports its reward in /logs/verifier/reward.txt.
echo "This is a template. Run the generated task to use its upstream tests." >&2
exit 1
