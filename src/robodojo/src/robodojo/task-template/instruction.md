# RoboDojo: $task_id

Control a bimanual ARX X5 robot in the RoboDojo simulator on the native
`$task_id` task of the `$dimension` dimension, layout seed group `$seed`.

The evaluation has 50 episodes. In every episode the simulator supplies the
original language instruction, one RGB frame per camera, and the measured robot
state. You answer each observation with one bounded end-effector motion for
both arms. The simulator decides success with its own task conditions and ends
each episode by itself.

Use the bridge client in this container:

```bash
python3 /opt/robodojo/bridge_client.py observe --wait 600
python3 /opt/robodojo/bridge_client.py act --wait 600 --response '<decision JSON>'
```

`observe` waits for the next observation and saves it under
`/tmp/robodojo-direct/<request_id>/` as `observation.json` plus one PNG per
camera. It reports `finished` once all episodes are done. `act` executes one
decision and returns the next observation, or `episode_end`.

A decision is a JSON object:

```json
{
  "request_id": "<from the current observation>",
  "mode": "eef",
  "steps": 5,
  "reason": "Visible evidence and purpose of this motion.",
  "target": {
    "left": {"position": [0.0, 0.0, 0.0], "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0], "gripper_closed": false},
    "right": {"position": [0.0, 0.0, 0.0], "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0], "gripper_closed": false}
  }
}
```

- Positions are in meters and orientations are unit quaternions in w, x, y, z
  order. Both refer to each arm's `link6` in the shared environment-origin
  frame, the same frame as `current_eef` in the observation.
- Each target must stay within 0.05 m and 0.35 rad of that arm's current
  measured pose. `steps` is 1 to 5 control steps at 25 Hz.
- `gripper_closed` commands the gripper. The reported gripper value is the last
  command, not a measured gap.
- A rejected decision executes nothing and can be corrected. There is no reset,
  rollback, or early stop, and object poses and rewards are not available.

Each canonical task evaluates 50 episodes; Generalization tasks combine 25
standard and 25 randomized episodes. The result is the native success rate and
the normalized native partial-progress score over all 50 episodes.
