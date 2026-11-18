"""CPU mock of the simulator side, for checking the plumbing without Isaac Sim.

This is not RoboDojo. It is an invented two-gripper table scene that draws its
camera pictures with NumPy, so that the bridge, the agent, a real vision model
and the verifier can be exercised on any machine without a GPU. The broker,
the episode loop, the action contract and the verifier are the real ones.

- ``scene``   The mock environment, its picture renderer and its motion model.
- ``service`` Entry point of the mock simulator container.
- ``solver``  A scripted player that needs no model. It is also the oracle.
- ``fake_openai`` A local fake of the OpenAI Chat Completions endpoint.
- ``local``   Runs the whole mock on one machine without Docker.

Mock results carry ``"mock": true`` and a task name outside the benchmark, and
aggregation refuses them. They say nothing about any RoboDojo task.
"""
