"""Resolve the four upstream Python modules used in the proposed paired runs."""
import hashlib
import importlib.util
import tempfile
import os
from pathlib import Path
import sys

MODULES = {
    "note_agent.py": "71e2967be83abc4d1e7cb2cd1b18f7aa926605460d8f02d967c6c01f5e5e6169",
    "environment_manager.py": "b76cf8b32ca68ff50a93bc3c66835604fe82861b347619d1812052fb0d8b174d",
    "web_actions.py": "54552acd97782918f74ae82fb5ba3f31b1ca332ad4f4281b540df7c232d375a1",
    "playwright_controller.py": "46e5ddaa65751822761820b681163f1ce98c681d8e22867ec13991776ebea3a5",
}


def load_upstream():
    root = Path(os.environ["STRESSWEB_UPSTREAM"]).resolve()
    for name, digest in MODULES.items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f"Upstream module differs from the specified revision: {name}")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(1, str(root))
    # The published f-strings contain unescaped literal JSON delimiters in two
    # examples. Both runners load the same derivative; interpolation of the OS,
    # viewport and shortcut keys is preserved. The downloaded source stays intact.
    original = (root / "note_agent.py").read_text().splitlines(keepends=True)
    for first, last in ((207,225), (268,284)):
        for i in range(first - 1, last):
            original[i] = original[i].replace("{ ", "{{ ").replace("} ", "}} ")
    corrected = "".join(original)
    if hashlib.sha256(corrected.encode()).hexdigest() != "ce09a62be316e80b263be97816b7dc6d84e220c7e90528b40bff3d0faa4bb5e9":
        raise ValueError("Unexpected change to the upstream prompt compatibility fix")
    with tempfile.TemporaryDirectory() as tmp:
        fixed = Path(tmp) / "note_agent.py"
        fixed.write_text(corrected)
        spec = importlib.util.spec_from_file_location("stressweb_note_policy", fixed)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module.NoteWebsiteAgent
