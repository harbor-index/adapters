"""Called by Harbor after the agent stops, inside this trial's broker."""
import json
import os
from pathlib import Path
from urllib.request import urlopen


def collect():
    path = Path(os.getenv("STB_TRACE_PATH", "/stb-trace/answer.json"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    port = int(os.getenv("STB_BROKER_PORT", "8090"))
    with urlopen(f"http://127.0.0.1:{port}/snapshot", timeout=60) as response:
        snapshot = json.load(response)
    if not isinstance(snapshot, dict) or "infrastructure_failure" not in snapshot or "raw" not in snapshot:
        raise RuntimeError("Incomplete response from broker")
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(snapshot, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


if __name__ == "__main__":
    collect()
