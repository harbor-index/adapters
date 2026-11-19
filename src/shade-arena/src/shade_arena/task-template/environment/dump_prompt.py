"""Extract the prompts from the original pipeline during task generation."""

import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))

from utils.pipeline_utils import PipelineUtils

spec = importlib.util.spec_from_file_location("task_definition", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
task = module.TaskDefinition()
system, user, _ = PipelineUtils(task).get_system_and_user_message()
print(json.dumps({
    "system": system,
    "user": user,
    "module": task.environment_class.__module__,
    "environment": task.environment_class.__name__,
}))
