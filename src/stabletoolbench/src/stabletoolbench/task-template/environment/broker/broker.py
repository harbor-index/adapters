"""A per-trial process owns all observations and persists the upstream trajectory."""
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

from native import contain, get_white_list, rapidapi_wrapper, standardize


def build_environment():
    query = json.loads(Path("query.json").read_text())
    args = SimpleNamespace(tool_root_dir="tools", toolbench_key="", rapidapi_key="",
                           use_rapidapi_key=False, api_customization=False,
                           max_observation_length=1024, observ_compress_method="truncate")
    if not os.environ["SERVICE_URL"].rstrip("/").endswith("/virtual"):
        raise ValueError("SERVICE_URL must identify a MirrorAPI /virtual endpoint")
    lookup = get_white_list(args.tool_root_dir)
    selected = contain([standardize(api["tool_name"]) for api in query["api_list"]], lookup)
    descriptions = [[item["standard_tool_name"], item["description"]] for item in selected]
    return query, rapidapi_wrapper(query, descriptions, None, args, process_id=1)


class Trial:
    def __init__(self, query, environment):
        self.query = query
        self.env = environment
        self.events = []
        self.closed = False
        self.failure = None

    def snapshot(self):
        messages = [{"role": "system", "content": self.env.task_description},
                    {"role": "user", "content": self.query["query"]}]
        chain = []
        for event in self.events:
            name, args, obs = event["name"], event["arguments"], event["observation"]
            messages.append({"role": "assistant", "function_call": {"name": name, "arguments": args}})
            messages.append({"role": "function", "name": name, "content": obs})
            chain.extend([{"node_type":"Action","description":name},
                          {"node_type":"Action Input","description":args,"observation":obs}])
        last = self.events[-1] if self.events else {}
        solved = last.get("code") == 3
        raw = {"answer_generation":{"valid_data":solved,
                                    "query":self.query["query"],
                                    "function":self.env.functions,
                                    "final_answer":last["arguments"] if solved else "",
                                    "train_messages":[messages]},
               "trys":[{"chain":chain}]}
        return {"query_id": self.query["query_id"], "raw": raw,
                "infrastructure_failure": self.failure}


    def call(self, name, arguments):
        if self.failure:
            raise RuntimeError("Trial has an infrastructure failure")
        if self.closed:
            raise ValueError("This trial already ended with Finish")
        try:
            observation, code = self.env.step(action_name=name, action_input=arguments)
            # The upstream wrapper reserves 5 for an HTTP timeout and 12 for
            # invalid HTTP status/JSON. Modeled tool errors use other codes.
            if code in (5, 12):
                raise RuntimeError(f"MirrorAPI transport status {code}")
            self.events.append({"name":name,"arguments":arguments,"observation":observation,"code":code})
            self.closed = code in (3, 4)
        except Exception as exc:
            self.failure = f"{type(exc).__name__} while processing a native tool action"
            if isinstance(exc, RuntimeError) and str(exc).startswith("MirrorAPI transport status "):
                self.failure = str(exc)
            raise RuntimeError(self.failure) from exc
        return {"observation":observation,"code":code}


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/snapshot" and self.client_address[0] == "127.0.0.1":
            self.reply(200, self.server.trial.snapshot())
            return
        if self.path != "/tools":
            self.reply(404, {"error":"unknown route"})
            return
        env = self.server.trial.env
        self.reply(200, {"task_description":env.task_description,"tools":env.functions})

    def do_POST(self):
        if self.path != "/call":
            self.reply(404, {"error":"unknown route"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size <= 0 or size > 1048576:
                raise ValueError("invalid request length")
            req = json.loads(self.rfile.read(size))
            if not isinstance(req, dict) or not isinstance(req.get("name"), str) or not isinstance(req.get("arguments"), str):
                raise ValueError("expected a function name and serialized arguments")
            result = self.server.trial.call(req["name"], req["arguments"])
        except (ValueError, KeyError) as exc:
            self.reply(400, {"error":str(exc)})
        except Exception as exc:
            self.log_error("MirrorAPI or ledger error: %s", type(exc).__name__)
            self.reply(502, {"error":"MirrorAPI call or trace persistence failed"})
        else:
            self.reply(200, result)


if __name__ == "__main__":
    query, env = build_environment()
    server = HTTPServer((os.getenv("STB_BROKER_BIND", "0.0.0.0"), int(os.getenv("STB_BROKER_PORT", "8090"))), Handler)
    server.trial = Trial(query, env)
    server.serve_forever()
