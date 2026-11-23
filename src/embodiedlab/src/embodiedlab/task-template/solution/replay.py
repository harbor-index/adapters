"""Oracle for an EmbodiedLab Harbor task: replay a recorded successful T0 run through the public tool API.

The recorded run is a successful agent run of this very instance. Every call it made (tool name and arguments,
nothing the agent did not send) is issued again in the same order, so the oracle uses the same interface as any
agent and is scored by the same evaluator. The only rewrite is the episode id: state ids ("<episode>:<tick>") and
evidence ids ("<episode>:obs:<n>", "<episode>:logger:<n>", ...) of the recorded run are mapped onto the new episode,
read from the first result that carries one. The simulator is deterministic for a given instance and call sequence,
so ticks and evidence counters normally repeat. Two exceptions are handled:
- The recorded run's simulator was at times further on than its own call log accounts for (a call the recording
  harness repeated after a timeout, e.g. a second 30 s wait). Each recorded call keeps the physics tick its result
  reported ("tick"); when the replay's result is behind it, the replay waits for the missing simulated time, so the
  apparatus evolves for as long as it did in the recorded run.
- A command the recorded run had accepted can still be refused as based on a stale robot state; it is then sent once
  more with the state id the replay last observed, as an agent observing before it acts would. Calls the recorded
  run itself had refused ("ok": false) are never retried.
"""
import json
import lzma
import sys

from embodiedlab_api import ToolError, connect

with lzma.open(sys.argv[1], "rt") as handle:
    calls = [json.loads(line) for line in handle if line.strip()]
old_episode = calls[0]["episode"]
env = connect(workspace="/home/agent/workspace")
new_episode = None


def rewrite(value):
    if isinstance(value, str):
        return value.replace(old_episode, new_episode) if new_episode else value
    if isinstance(value, list):
        return [rewrite(v) for v in value]
    if isinstance(value, dict):
        return {k: rewrite(v) for k, v in value.items()}
    return value


def tick_of(state_id):
    tail = str(state_id).rsplit(":", 1)[-1]
    return int(tail) if tail.isdigit() else 0


READS = {"get_robot_state", "get_observation", "solve_ik", "get_task_spec"}


def reads_only(name):
    return name in READS or name.startswith("export_")


# For a read: the tick the recorded run's simulator was at, from the first result of this run of reads that reports one.
target = [None] * len(calls)
ahead = None
for i in range(len(calls) - 1, -1, -1):
    if not reads_only(calls[i]["tool"]):
        ahead = None
        continue
    if calls[i].get("tick") is not None:
        ahead = calls[i]["tick"]
    target[i] = ahead

submitted = False
latest_state = None
hz = 240.0
for index, row in enumerate(calls):
    name, arguments = row["tool"], rewrite(row["arguments"])
    behind = (target[index] or 0) - tick_of(latest_state) if latest_state else 0
    if behind > 0:
        print(f"{index:04d} {name}: the recorded run was {behind} ticks further on; waiting {behind / hz:.3f} s first",
              flush=True)
        while behind > 0:
            step = min(behind, int(30 * hz))
            try:
                latest_state = env.call("wait", seconds=step / hz)["state_id"]
            except ToolError as exc:
                print(f"{index:04d} wait: refused: {exc}", flush=True)
                break
            behind -= step
    try:
        result = env.call(name, **arguments)
    except ToolError as exc:
        stale = "stale" in str(exc) and "expected_state_id" in arguments and latest_state
        if not (stale and row.get("ok", True) and arguments["expected_state_id"] != latest_state):
            # The recorded run had refused calls too (an agent's mistakes are part of its run); keep going.
            print(f"{index:04d} {name}: refused: {exc}", flush=True)
            continue
        arguments = dict(arguments, expected_state_id=latest_state)
        try:
            result = env.call(name, **arguments)
            print(f"{index:04d} {name}: resent with the current state id", flush=True)
        except ToolError as exc2:
            print(f"{index:04d} {name}: refused: {exc2}", flush=True)
            continue
    if isinstance(result, dict) and ":" in str(result.get("state_id") or ""):
        latest_state = result["state_id"]
        hz = float(result.get("physics_hz") or hz)
    if new_episode is None and isinstance(result, dict):
        sid = result.get("state_id") or result.get("evidence_id") or ""
        if ":" in sid:
            new_episode = sid.split(":", 1)[0]
            print(f"episode {old_episode} -> {new_episode}", flush=True)
    print(f"{index:04d} {name}: ok", flush=True)
    if name == "submit_result":
        print(json.dumps(result, default=str)[:2000], flush=True)
        submitted = True
        break
sys.exit(0 if submitted else 1)
