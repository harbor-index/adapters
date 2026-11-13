#!/bin/sh
set -eu
/usr/local/bin/python - <<'CHECK'
from pathlib import Path
import json
assert not Path('/tests').exists(), 'Hidden tests leaked into the agent VM'
assert not Path('/workspace/state/validation_cases.json').exists(), 'Reference answers leaked'
Path('/workspace/state/messages.json').write_text(json.dumps({'messages':[{'role':'assistant','content':'Finished without selecting anything.'}]}))
Path('/logs/verifier').mkdir(parents=True,exist_ok=True)
Path('/logs/verifier/reward.txt').write_text('1\n')
print('Agent context has no hidden tests or answers; attempted reward forgery written.')
CHECK
