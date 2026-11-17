"""Direct mode: a vision model chooses bounded end-effector targets for RoboDojo.

The package is split by where each module runs:

* ``contract``, ``prompts`` and ``bridge_client`` use only the standard library,
  because they run on the Harbor host or in the lightweight agent container.
* ``broker`` and ``supervisor`` use only the standard library and run inside the
  simulator container, next to the native RoboDojo evaluation.
* ``kinematics``, ``episode`` and ``policy`` additionally need NumPy and SciPy,
  which the pinned simulator image already provides.
"""
