#!/usr/bin/env python3
import argparse
import json
import os
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser(description="Call one StableToolBench function")
    actions = parser.add_subparsers(dest="command", required=True)
    actions.add_parser("list")
    call = actions.add_parser("call")
    call.add_argument("name")
    call.add_argument("arguments")
    args = parser.parse_args()
    address = os.environ.get("STB_BROKER_URL", "http://broker:8090").rstrip("/")
    if args.command == "list":
        req = Request(address + "/tools")
    else:
        body = json.dumps({"name": args.name, "arguments": args.arguments}).encode()
        req = Request(address + "/call", data=body, headers={"Content-Type":"application/json"})
    with urlopen(req, timeout=300) as response:
        print(json.dumps(json.load(response), ensure_ascii=False))


if __name__ == "__main__":
    main()
