#!/bin/bash
set -eu
python3 "$(dirname "$0")/reference.py" @QUERY_ID@ @MODE@
