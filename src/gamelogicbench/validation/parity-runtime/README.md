# Fixed ARM64 model runtime

This is the runtime recipe used for the local GPT-5 Mini parity experiment. It extends the adapter's Godot 4.4 image with the same official Codex 0.144.1 binary on both sides. The binary is downloaded during preparation; agent containers need access only to the approved model API.

Run from this directory on an ARM64 Linux Docker backend. First build `gamelogicbench-runtime:godot-4.4` using the adapter's existing `runtime/Dockerfile`. Provision the pinned CLI files:

```bash
python3 - <<'PY'
from pathlib import Path
import base64
import hashlib
import io
import json
import shutil
import tarfile
import urllib.request

record = json.loads(Path("provenance.json").read_text())
with urllib.request.urlopen(record["tarball"], timeout=120) as response:
    archive = response.read()
actual = "sha512-" + base64.b64encode(hashlib.sha512(archive).digest()).decode()
assert actual == record["integrity"], "Unexpected npm archive checksum"
with tarfile.open(fileobj=io.BytesIO(archive)) as package:
    for name in ("codex", "rg"):
        matches = [member for member in package.getmembers()
                   if member.isfile() and member.name.endswith("/" + name)]
        assert len(matches) == 1, name
        with package.extractfile(matches[0]) as source, Path(name).open("wb") as target:
            shutil.copyfileobj(source, target)
        Path(name).chmod(0o755)
assert hashlib.sha256(Path("codex").read_bytes()).hexdigest() == record["codex_sha256"]
PY

docker build -t gamelogicbench-parity:codex-0.144.1 .
docker run --rm --network none gamelogicbench-parity:codex-0.144.1 codex --version
```

The output must be `codex-cli 0.144.1`. Downloaded binaries are local build inputs, not repository contents. The recorded experiment used Linux ARM64; this archive is not an AMD64 executable. The adapter's ordinary Oracle runtime continues to support both architectures.
