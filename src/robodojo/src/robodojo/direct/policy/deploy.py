"""Native deploy hook: one Direct episode per call, one environment at a time."""

import os
import sys

sys.path.insert(0, os.environ.get("HARBOR_ROBODOJO_HOME", "/opt/harbor-robodojo"))

from robodojo.direct.episode import run_episode


def eval_one_episode(TASK_ENV, model_client):
    run_episode(TASK_ENV, model_client.call)
