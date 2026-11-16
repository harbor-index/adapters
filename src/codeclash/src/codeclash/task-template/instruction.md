Run the native CodeClash PvP task for {{ARENA_NAME}} through the custom
`codeclash.harbor_agent:CodeClashAgent` Harbor coordinator.

The pinned task configuration in `/codeclash/task-config.json` defines the arena,
players ({{PLAYERS}}), prompts, round budget, simulation budget and game options.
Each native coding player receives its original rendered CodeClash prompt and a
persistent codebase. Improve the program described by that prompt, inspect the
previous round's feedback and submit each edit round through the native agent.
The coordinator executes baseline round 0 followed by editing and competition.
Transparent PvP, when explicitly configured, exposes prior opponent snapshots.

Native game logs and edit history are exported to `/codeclash/evidence`. A separate
verifier checks the handoff and reports native player scores. Rounds and matches
are records within this arena task. The smoke profile uses scripted editors only
and produces diagnostic metrics, never oracle or model-parity evidence.

This task requires the CodeClash coordinator; a generic single-agent CLI invocation
alone cannot implement the native multi-player tournament protocol.
