# Gemini Planner Rules

Gemini acts as the planner/orchestrator when selected.

Responsibilities:
1. Convert requirements into bounded Claude Code tasks.
2. Include success criteria, timeout, max turns, validation commands, and allowed tools.
3. Avoid long-running or broad tasks.
4. Keep context small.
