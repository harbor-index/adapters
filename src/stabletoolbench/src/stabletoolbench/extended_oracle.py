"""Answer the additional tool-use queries from fresh, agent-visible observations."""
import json
from pathlib import Path
import re

from oracle import call, describe


def response_payload(observation):
    try:
        envelope = json.loads(observation)
    except json.JSONDecodeError:
        # The native observation budget can cut off the outer JSON. Use only
        # the visible part, with an explicit indication that it may be partial.
        return describe(observation), True
    if envelope["error"]:
        raise ValueError(envelope["error"])
    payload = envelope["response"]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            pass
    return payload, False


def display(payload):
    if payload in (None, "", [], {}):
        return "The lookup returned no entries."
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, ensure_ascii=False, indent=2)


def visible_jokes(payload):
    if isinstance(payload, dict):
        if payload.get("setup"):
            yield payload
        else:
            for value in payload.values():
                yield from visible_jokes(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from visible_jokes(item)
    elif isinstance(payload, str):
        # A clipped array can still contain some complete joke records.
        for match in re.finditer(r"\{", payload):
            try:
                item, _ = json.JSONDecoder().raw_decode(payload[match.start():])
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and item.get("setup"):
                yield item


def anime_answer(records):
    catalog = []
    observed = []
    for name, observation in records:
        payload, clipped = response_payload(observation)
        jokes = []
        for item in visible_jokes(payload):
            text = " ".join(str(part) for part in (item.get("setup"), item.get("punchline")) if part)
            series = str(item.get("joke_anime") or item.get("anime") or "")
            observed.append((series, text))
            jokes.append(f"- {text}" + (f" ({series})" if series else ""))
        if name == "get_all_jokes_for_anime_jokes":
            catalog = jokes or [display(payload)]
            if clipped:
                catalog.append("The tool response ended at the observation limit.")
    one_piece = [text for series, text in observed if "one piece" in (series + " " + text).lower()]
    how = [text for _, text in observed if re.search(r"\bhow\b", text, flags=re.IGNORECASE)]
    sections = []
    for label, texts in (("One Piece jokes", one_piece), ('Jokes containing "how"', how)):
        listing = "\n".join("- " + text for text in dict.fromkeys(texts))
        sections.append(label + "\n" + (listing or "No matching joke was visible in the search or catalog."))
    sections.append("Browse the jokes returned by the full catalog lookup\n" + "\n".join(catalog))
    return "\n\n".join(sections)


def cinema_answer(records):
    sections = []
    for name, observation in dict.fromkeys(records):
        payload, clipped = response_payload(observation)
        if name == "api_all_for_movie_showtimes":
            label = "Cinemas NOS listings across all movies"
            if isinstance(payload, str) and not re.search(r"\b\d{1,2}:\d{2}\b", payload):
                label += " (the catalog lookup supplied no individual showtimes)"
        else:
            label = "Movie details and sessions reported for IMDb tt6105098"
        section = label + "\n" + display(payload)
        if clipped:
            section += "\nThe tool response ended at the observation limit."
        sections.append(section)
    return "\n\n".join(sections)


def airport_answer(records):
    sections = []
    for name, observation in records:
        payload, clipped = response_payload(observation)
        label = ("Airports returned by the Dallas city lookup"
                 if name == "get_airports_by_city_for_airports_finder"
                 else "DFW airport details returned by the IATA lookup")
        section = label + "\n" + display(payload)
        if clipped:
            section += "\nThe tool response ended at the observation limit."
        sections.append(section)
    return "\n\n".join(sections)


def main():
    plan = json.loads(Path(__file__).with_name("calls.json").read_text())
    records = []
    for action in plan:
        name = action["name"]
        result = call(name, action["arguments"])
        if result["code"] != 0:
            raise RuntimeError(f"{name} returned native status {result['code']}")
        records.append((name, result["observation"]))

    tool = plan[0]["name"].rsplit("_for_", 1)[-1]
    if tool == "anime_jokes":
        answer = anime_answer(records)
    elif tool == "movie_showtimes":
        answer = cinema_answer(records)
    elif tool == "airports_finder":
        answer = airport_answer(records)
    else:
        raise ValueError(f"Unexpected reference tool: {tool}")

    final = {"return_type":"give_answer","final_answer":answer}
    if call("Finish", json.dumps(final, ensure_ascii=False))["code"] != 3:
        raise RuntimeError("Upstream rejected Finish")


if __name__ == "__main__":
    main()
