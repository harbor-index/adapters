"""Native policy-server model that relays Direct decisions from the Harbor agent.

The native simulator client calls these methods through the unchanged
XPolicyLab policy protocol. No inference happens here: ``direct_observe``
publishes an observation to the broker and ``direct_poll`` returns the decision
that the agent submitted for it, if any.
"""

import os
import sys
import threading

sys.path.insert(0, os.environ.get("HARBOR_ROBODOJO_HOME", "/opt/harbor-robodojo"))

from robodojo.direct.broker import Broker, build_observation, encode_png, make_server
from robodojo.direct.episode import HARBOR_KEY

try:
    from XPolicyLab.model_template import ModelTemplate
except ImportError:  # CPU tests run without the simulator image.
    ModelTemplate = object


def camera_png(color) -> bytes:
    """Losslessly encode one decoded native camera frame."""
    import numpy as np

    frame = np.asarray(color)
    if frame.ndim != 3 or frame.shape[2] < 3 or frame.dtype != np.uint8:
        raise ValueError(f"Unexpected native camera frame: shape={frame.shape}, dtype={frame.dtype}")
    frame = np.ascontiguousarray(frame[..., :3])
    return encode_png(frame.shape[1], frame.shape[0], frame.tobytes())


class Model(ModelTemplate):
    def __init__(self, model_cfg, *, broker=None, serve=True):
        self.model_cfg = model_cfg
        self.broker = broker or Broker(os.environ.get("HARBOR_ROBODOJO_RESULTS", "/harbor-robodojo/results"))
        self.server = None
        if serve:
            self.server = make_server(
                self.broker,
                os.environ.get("HARBOR_ROBODOJO_BRIDGE_HOST", "0.0.0.0"),
                int(os.environ.get("HARBOR_ROBODOJO_BRIDGE_PORT", "19100")),
            )
            threading.Thread(target=self.server.serve_forever, name="harbor-bridge", daemon=True).start()

    def reset(self):
        return None

    def update_obs(self, obs):
        raise RuntimeError("HarborDirect has no autonomous policy; use the direct_* calls")

    def get_action(self):
        raise RuntimeError("HarborDirect has no autonomous policy; use the direct_* calls")

    def direct_begin(self, meta):
        return self.broker.begin_episode(meta)

    def direct_observe(self, native):
        harbor = native[HARBOR_KEY]
        observation = build_observation(
            native, step_id=int(harbor["step_id"]),
            max_episode_steps=int(harbor["max_episode_steps"]),
        )
        images = {name: camera_png(camera["color"]) for name, camera in native["vision"].items()}
        if not images:
            raise ValueError("Native observation contains no camera frames")
        posted = self.broker.post_observation(
            harbor["episode_id"], observation, images, harbor.get("previous_result"),
        )
        return {"request_id": posted["request_id"], "current_eef": observation["current_eef"]}

    def direct_poll(self, query):
        return self.broker.poll_decision(query["request_id"])

    def direct_end(self, summary):
        return self.broker.end_episode(summary["episode_id"], summary)

    def direct_abandon(self, report):
        return self.broker.abandon_episode(report["episode_id"], report.get("error", ""))
