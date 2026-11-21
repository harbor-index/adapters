"""Regression checks for action input and capture integrity in a generated task."""
import asyncio
import base64
import io
from pathlib import Path
import sys
import tempfile
from unittest.mock import AsyncMock

from PIL import Image


def must_reject(call):
    try:
        call()
    except (ValueError, FileNotFoundError):
        return
    raise AssertionError("Invalid input was accepted")


async def check(task):
    sys.path[:0] = [str(task/"environment/browser"), str(task/"tests")]
    from action_input import validate_action
    from score import verified_capture
    from service import Session

    url = "http://localhost:3000"
    for amount in ("12", True, 2.5, None, {}):
        must_reject(lambda: validate_action({"action_type":"SCROLL",
                      "parameters":{"direction":"down", "amount":amount}}, url))
    for x in ("12", True, float("inf"), float("nan"), {}):
        must_reject(lambda: validate_action({"action_type":"CLICK","parameters":{"x":x,"y":10}}, url))
    for selector in ("unknown=button", "css=button >> unknown=label"):
        must_reject(lambda: validate_action({"action_type":"CLICK","parameters":{"selector":selector}}, url))
    for selector in ("css=button", "text=Open", "button:has-text('Open') >> nth=-1", "input[name='tag']"):
        validate_action({"action_type":"CLICK","parameters":{"selector":selector}}, url)
    validate_action({"action_type":"SCROLL","parameters":{"direction":"down","amount":30}}, url)
    validate_action({"action_type":"CLICK","parameters":{"x":1.5,"y":2}}, url)
    png = io.BytesIO()
    Image.new("RGB", (2,2)).save(png,format="PNG")
    obs = {"raw_dom":"<html><body>fixture</body></html>", "page_info":{"url":url},
           "screenshot":base64.b64encode(png.getvalue()).decode()}
    for actions in ([], ["FAIL"], ["RELOAD"], ["RELOAD","FAIL"], ["DONE"]):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            session = Session("fixture", folder, 3000, 5100)
            session.observation = AsyncMock(return_value=obs)
            session.controller.execute_action = AsyncMock(return_value={"success":True})
            await session.record(None, None)
            for kind in actions:
                await session.call("/act", {"action_type":kind,"parameters":{}})
            summary = verified_capture(folder)
            assert summary["steps"] == len(actions)
            assert bool(list(folder.glob("final_*_raw.html"))) == (not actions or actions[-1] == "DONE")
            snapshot = next(folder.glob("initial_*_raw.html"))
            saved = snapshot.read_bytes()
            snapshot.write_bytes(saved[:-3])
            must_reject(lambda: verified_capture(folder))
            snapshot.write_bytes(saved)
            traj = folder/"traj.jsonl"
            traj.unlink()
            must_reject(lambda: verified_capture(folder))
    with tempfile.TemporaryDirectory() as temp:
        session = Session("fixture", Path(temp), 3000, 5100)
        session.observation = AsyncMock(return_value=obs)
        session.controller.execute_action = AsyncMock(return_value={"success":True})
        await session.record(None, None)
        session.observation.side_effect = RuntimeError("fixture capture interrupted")
        try:
            await session.call("/act", {"action_type":"RELOAD","parameters":{}})
        except RuntimeError:
            pass
        else:
            raise AssertionError("Interrupted capture unexpectedly succeeded")
        must_reject(lambda: verified_capture(Path(temp)))


if __name__ == "__main__":
    asyncio.run(check(Path(sys.argv[1]).resolve()))
    print("Action validation and capture-integrity checks passed.")
