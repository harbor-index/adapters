"""Reference workflows through the same browser actions available to all agents."""
import base64
import io
import json
import os
import sys
import time
from urllib.request import Request, urlopen

BASE = os.environ.get("STRESSWEB_BROWSER_URL", "http://browser:8765")

def request(endpoint, data=None):
    payload = json.dumps(data).encode() if data is not None else None
    with urlopen(Request(BASE + endpoint, data=payload, headers={"Content-Type":"application/json"}), timeout=90) as r:
        return json.load(r)

def close_popups():
    # The two popup types have the same blue OK and gray Cancel buttons.
    # Orange warning text visually identifies the variant that requests Cancel.
    from PIL import Image
    for _ in range(4):
        raw = base64.b64decode(request("/screenshot")["screenshot"])
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        w, h = im.size
        pixels = im.load()
        blue = {(0, 123, 255), (0, 86, 179)}
        points = [(x,y) for y in range(h//2, 3*h//4, 3) for x in range(w//2, 7*w//10, 3)
                  if pixels[x,y] in blue]
        if len(points) < 350:
            return
        x1, x2 = min(x for x,y in points), max(x for x,y in points)
        y1, y2 = min(y for x,y in points), max(y for x,y in points)
        if not (105 <= x2-x1 <= 140 and 34 <= y2-y1 <= 55):
            return
        warning = any(pixels[x,y] == (255,107,0) for x in range(w//3, x2, 2)
                      for y in range(h//5, y1, 2))
        label = "Cancel" if warning else "OK"
        request("/act", {"action_type":"CLICK","parameters":{
            "selector":f'button:text-is("{label}"):visible >> nth=-1'}})
    raise RuntimeError("Repeated confirmation dialogs did not clear")

def act(kind, **parameters):
    if sys.argv[2] == "perturbed":
        close_popups()
    result = request("/act", {"action_type":kind, "parameters":parameters})
    if sys.argv[2] == "perturbed" and kind not in {"DONE", "FAIL"}:
        close_popups()
    return result

def observed():
    return request("/observe")

def click(selector, complete, mode):
    for attempt in range(15 if mode == "failed" else 6):
        if complete(observed()):
            return
        act("CLICK", selector=selector, click_count=2 if mode.startswith("semantic") else 1)
        if complete(observed()):
            return
        if mode == "failed":
            time.sleep(3)
    if not complete(observed()):
        raise RuntimeError(f"Click did not reach the expected page state: {selector}")

def fill(selector, value):
    for attempt in range(8):
        result = act("FILL", selector=selector, text=value)
        if result["success"] and value in observed()["dom"]:
            return
    raise RuntimeError(f"Could not fill {selector}")

def document(mode):
    click(".action-primary", lambda obs: 'class="title-input"' in obs["dom"], mode)
    fill(".title-input", "Project Alpha Notes")
    text = "Initial project meeting scheduled for January 20, 2026"
    # Apply the first-line style to its first word before completing the line.
    # A real input disruption may erase text or clear the active formatting.
    for attempt in range(10):
        act("FILL", selector=".editor-content", text="Initial")
        dom = observed()["dom"]
        if not any(f"<{tag}>Initial</{tag}>" in dom for tag in ("b", "strong")):
            act("HOTKEY", keys="Control+a")
            act("HOTKEY", keys="Control+b")
            dom = observed()["dom"]
        if not any(f"<{tag}>Initial</{tag}>" in dom for tag in ("b", "strong")):
            continue
        act("FILL", selector=".editor-content", text=text)
        dom = observed()["dom"]
        if any(f"<{tag}>{text}</{tag}>" in dom for tag in ("b", "strong")):
            break
    else:
        raise RuntimeError("Required bold first line was not visible")
    click('.editor-header-right .btn-primary', lambda o: "/document/" in o["page_info"]["url"] and "Project Alpha Notes" in o["dom"], mode)

def folder(mode):
    click('button.action-card:has-text("Workspace")', lambda o: "/workspace" in o["page_info"]["url"], mode)
    click('.workspace-card:has-text("Personal Workspace")', lambda o: "/workspaces/" in o["page_info"]["url"], mode)
    click('button:has-text("New Folder")', lambda o: 'placeholder="Enter folder name..."' in o["dom"], mode)
    fill('input[placeholder="Enter folder name..."]', "Sprint Documents")
    click('.modal-footer .btn-primary', lambda o: 'placeholder="Enter folder name..."' not in o["dom"] and
          "Sprint Documents" in o["dom"], mode)

if __name__ == "__main__":
    query, mode = sys.argv[1:]
    {"1": document, "4": folder}[query](mode)
    act("DONE")
