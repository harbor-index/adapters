"""Validate public action data against the pinned StressWeb action descriptions."""
import re
from urllib.parse import urlsplit

from web_actions import ACTION_SPACE

SPECS = {item["action_type"]: item["parameters"] for item in ACTION_SPACE}
# The upstream action protocol uses CSS selectors and Playwright text locators.
# Explicit engines must be declarative, including within a chained locator.
ENGINES = {"css", "css:light", "text", "text:light", "xpath", "xpath:light",
           "id", "id:light", "nth", "role", "data-testid", "data-test-id", "data-test"}
ENGINE_NAME = re.compile(r"^\s*([a-zA-Z_0-9+:*-]+)\s*=")


def validate_action(body, frontend):
    if not isinstance(body, dict) or not isinstance(body.get("action_type"), str):
        raise ValueError("An action_type string is required")
    kind = body["action_type"]
    if kind not in SPECS:
        raise ValueError("Unsupported action")
    params = body.get("parameters", {})
    if not isinstance(params, dict) or params.keys() - SPECS[kind].keys():
        raise ValueError("Unknown parameters for this action")
    for key, spec in SPECS[kind].items():
        if key not in params:
            if not spec["optional"]:
                raise ValueError(f"Missing {key}")
            continue
        value = params[key]
        dtype = spec["type"]
        if dtype is float:
            if type(value) not in (int, float) or not (-1e308 <= value <= 1e308):
                raise ValueError(f"{key} must be a finite number")
        elif type(value) is not dtype:
            raise ValueError(f"{key} must be a {dtype.__name__}")
        bounds = spec.get("range")
        if bounds:
            if dtype in (int, float):
                if not (min(bounds) <= value <= max(bounds)):
                    raise ValueError(f"{key} outside its documented range")
            elif value not in bounds:
                raise ValueError(f"Unsupported {key}")
        if key in {"amount", "index"} and not (-2**31 <= value < 2**31):
            raise ValueError(f"{key} exceeds the browser's integer range")
        if key == "selector":
            for part in value.split(">>"):
                prefix = ENGINE_NAME.match(part.strip())
                if prefix and prefix.group(1).removeprefix("*") not in ENGINES:
                    raise ValueError("Unsupported locator engine")
    if kind == "NAVIGATE":
        target = urlsplit(params["url"])
        if f"{target.scheme}://{target.netloc}" != frontend:
            raise ValueError("Navigation must stay on the Note website")
    return {"action_type": kind, "parameters": params}
