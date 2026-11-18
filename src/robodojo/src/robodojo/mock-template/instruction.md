# RoboDojo adapter mock: reach targets

This is a mock, not a RoboDojo task. A small CPU service draws an invented table
scene instead of running the simulator. It exists to check the bridge, the
agent, and the verifier without a GPU. Its reward says nothing about RoboDojo.

The evaluation has $episodes episodes, layout seed group `$seed`. In every
episode the service supplies an instruction, one RGB frame per camera, and the
measured gripper state. You answer each observation with one bounded
end-effector motion for both grippers. The service decides success and ends
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
  order, in the same frame as `current_eef` in the observation.
- Each target must stay within 0.05 m and 0.35 rad of that gripper's current
  measured pose. `steps` is 1 to 5 control steps at 25 Hz.
- A rejected decision executes nothing and can be corrected.

The result is the success rate and the mean score over all $episodes episodes of
the mock scene.
