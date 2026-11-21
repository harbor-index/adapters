#!/usr/bin/env python3
import argparse
import base64
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen

def request(path, payload=None):
    body = json.dumps(payload).encode() if payload is not None else None
    req = Request(os.environ.get("STRESSWEB_BROWSER_URL", "http://browser:8765") + path,
                  data=body, headers={"Content-Type":"application/json"})
    with urlopen(req, timeout=90) as response:
        return json.load(response)

def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("observe")
    sub.add_parser("actions")
    sub.add_parser("screenshot").add_argument("path", type=Path)
    sub.add_parser("act").add_argument("json_action")
    args = parser.parse_args()
    if args.command == "screenshot":
        args.path.write_bytes(base64.b64decode(request("/screenshot")["screenshot"]))
    elif args.command == "act":
        result = request("/act", json.loads(args.json_action))
        print(json.dumps(result))
        if not result.get("success"):
            raise SystemExit(1)
    else:
        result = request("/" + args.command)
        print(json.dumps(result, ensure_ascii=False))

if __name__ == "__main__":
    main()
