# RoboDojo: $task_id

Evaluate your already-running XPolicyLab robot policy on the native RoboDojo
`$task_id` task in the `$dimension` dimension, with simulator seed `$seed`.
The simulator supplies the original language instructions, camera observations,
robot state, and reset/action protocol to your policy server.

Write `/workspace/submission.json` containing exactly these fields:

```json
{"policy_name": "YOUR_XPOLICYLAB_MODULE", "host": "YOUR_POLICY_SERVER", "port": 9999}
```

`policy_name` must name an existing module in
`/workspace/RoboDojo/XPolicyLab/policy/`, with its original `deploy.py` and
`deploy.yml`. The server must already be running and reachable from this
container. Use a network address reachable from Docker; `127.0.0.1` refers to
this container. Keep the same policy checkpoint and policy RNG settings across
all tasks and repetitions. The seed above selects simulator layouts, not the
server's RNG seed.

The verifier runs the official simulator against this endpoint. Each canonical
task evaluates 50 episodes; Generalization combines 25 base and 25 randomized
episodes. It reports native success rate and normalized partial-progress score.
Do not write result files or change the simulator, assets, or deployment adapters.

This draft adapter evaluates external robot policies. The Harbor agent submits
the endpoint; the external policy performs the robot manipulation.
