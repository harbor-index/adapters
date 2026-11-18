"""Direct mode: a vision model chooses bounded end-effector targets for RoboDojo.

The package is split by where each module runs:

* ``contract``, ``prompts`` and ``bridge_client`` use only the standard library,
  because they run on the Harbor host or in the lightweight agent container.
* ``player`` is the decision loop. It runs wherever the model is called: inside
  the Harbor agent or in the original-side driver. It needs the model client
  (LiteLLM) only when it calls a model.
* ``broker`` and ``supervisor`` use only the standard library and run inside the
  simulator container, next to the native RoboDojo evaluation.
* ``kinematics``, ``episode`` and ``policy`` additionally need NumPy and SciPy,
  which the pinned simulator image already provides.
"""
