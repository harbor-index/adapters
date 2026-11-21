"""Replay published action inputs and construct Finish from current observations."""
import json
import os
import re
from html.parser import HTMLParser
from pathlib import Path
import subprocess


def call(name, arguments):
    client = os.getenv("STB_TOOL_CLIENT", "stb-tool")
    out = subprocess.check_output([client, "call", name, arguments], text=True)
    return json.loads(out)


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, text):
        if text.strip() and text.strip() != ",":
            self.parts.append(text.strip())


def readable(payload):
    if isinstance(payload, str) and re.search(r"<[a-zA-Z]+[ >]", payload):
        parser = VisibleText()
        parser.feed(payload)
        if parser.parts:
            return ", ".join(dict.fromkeys(parser.parts))
    return payload


def describe(observation):
    # Upstream may truncate the outer JSON at 1024 characters. Preserve the
    # visible payload if truncation cuts a string or a nested array in half.
    try:
        envelope = json.loads(observation)
    except json.JSONDecodeError:
        if not observation.endswith("...") or '"error": ""' not in observation:
            raise ValueError("Invalid or unsuccessful observation")
        fragment = observation.partition('"response":')[2].strip()
        if fragment.startswith('"'):
            # Decode complete characters in a JSON string whose closing quote
            # was truncated. A trailing partial escape is not visible data.
            prefix = fragment[:-3]
            for cut in range(0, 7):
                try:
                    text = json.loads((prefix[:-cut] if cut else prefix) + '"')
                    return readable(text)
                except json.JSONDecodeError:
                    continue
        return fragment or observation
    if envelope.get("error") or envelope.get("response") in ("", None, [], {}):
        raise ValueError(f"The tool returned no usable data: {observation}")
    payload = envelope["response"]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return readable(payload)
    if isinstance(payload, list):
        return ", ".join(render(item) for item in payload)
    return render(payload)


def render(item):
    if isinstance(item, dict):
        return ", ".join(f"{key}: {value}" for key, value in item.items())
    return str(item)


def format_result(name, observation):
    text = describe(observation)
    if name != "popularsitesforquery_for_keyword_analysis":
        return text
    # A native 1,024-character observation can stop halfway through the
    # first anchor's query string. Its complete hostname is still visible.
    hosts = re.findall(r"(?<![\w.-])(?:https?://)?((?:[a-z0-9-]+\.)+[a-z]{2,24})(?![\w.-])",
                       observation, flags=re.IGNORECASE)
    if hosts:
        sites = list(dict.fromkeys(host.lower() for host in hosts))
        return ", ".join(sites)
    return text


def main():
    plan = json.loads(Path(__file__).with_name("calls.json").read_text())
    headings = {"popularsitesforquery_for_keyword_analysis":"Popular sites for birthday party ideas",
                "querykeywords_for_keyword_analysis":"Main keywords for birthday party ideas",
                "get_channel_clips_for_kick_com_api_kick_api":"Clips from gmhikaru",
                "get_channel_details_for_kick_com_api_kick_api":"gmhikaru channel details"}
    parts = []
    for action in plan:
        name = action["name"]
        result = call(name, action["arguments"])
        if result["code"] != 0:
            raise RuntimeError(f"{name} returned native status {result['code']}")
        parts.append(headings[name] + ": " + format_result(name, result["observation"]))
    final = {"return_type":"give_answer","final_answer":"\n\n".join(parts)}
    if call("Finish", json.dumps(final, ensure_ascii=False))["code"] != 3:
        raise RuntimeError("Upstream rejected Finish")


if __name__ == "__main__":
    main()
