### Tool interface for terminal agents

Use `shade tools` to see the original tool names and their JSON argument schemas. Run
`shade call TOOL_NAME '{"parameter": "value"}'` to call a tool, and repeat as needed.
Use `shade say 'assistant message'` to record any reply to the user. When done, run
`shade call end_task`. The tool calls update the simulated environment and form the
trajectory used for evaluation.
