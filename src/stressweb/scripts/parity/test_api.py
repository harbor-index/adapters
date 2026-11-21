"""OpenAI-compatible implementation of the client imported by upstream note_agent."""
import json
import os
from urllib.request import Request, urlopen


def call_openai(messages, model, max_tokens):
    endpoint = os.environ["STRESSWEB_MODEL_BASE_URL"].rstrip("/") + "/chat/completions"
    body = json.dumps({"model": model, "messages": messages, "max_tokens": max_tokens}).encode()
    req = Request(endpoint, data=body, headers={
        "Authorization": "Bearer " + os.environ["STRESSWEB_MODEL_API_KEY"],
        "Content-Type": "application/json",
    })
    with urlopen(req, timeout=120) as response:
        result = json.load(response)
    text = result["choices"][0]["message"]["content"]
    if not isinstance(text, str):
        raise ValueError("The model must return text in chat-completions format")
    return text
