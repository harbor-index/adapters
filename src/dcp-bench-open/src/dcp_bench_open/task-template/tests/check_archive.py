"""Check transport integrity before importing the image into the private daemon."""
import hashlib
import json
from pathlib import Path
case = json.loads(Path('/tests/case.json').read_text())
hash_value = hashlib.sha256()
with Path('/tests/solver-image.tar').open('rb') as stream:
    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
        hash_value.update(chunk)
if hash_value.hexdigest() != case['solver_archive_sha256']:
    raise SystemExit('Native solver image archive checksum mismatch')
