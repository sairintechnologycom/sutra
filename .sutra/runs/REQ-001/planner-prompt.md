You are the Sutra planning engine using gemini.

Convert the requirement into a bounded task plan for Claude Code. Return ONLY valid JSON.

Hard requirements:
- Each task must be small and bounded.
- Each task must include id, title, status, agent, model, timeout_seconds, max_turns, max_budget_usd, allowed_tools, validation_commands, success_criteria, context_files.
- status must be "pending".
- agent must be "claude-code".
- timeout_seconds must be <= 1800.
- max_turns must be <= 6.
- Do not create a single long-running task.
- Include a first inspection task and a final documentation/progress task.

Return this JSON shape:
{
  "title": "...",
  "risk": "low|medium|high",
  "tasks": [
    {
      "id": "T001",
      "title": "...",
      "status": "pending",
      "agent": "claude-code",
      "model": "sonnet",
      "timeout_seconds": 300,
      "max_turns": 2,
      "max_budget_usd": 0.25,
      "allowed_tools": ["Read", "Bash(git status *)", "Bash(git diff *)"],
      "validation_commands": ["git status --short"],
      "success_criteria": ["..."],
      "context_files": ["CLAUDE.md", "docs/progress.md"]
    }
  ]
}

Requirement:
# Requirement: Add Student Progress Dashboard

## Goal
Build a dashboard that shows progress by subject, topic, quiz score, and mastery level.

## Expected Outcome
- Dashboard page created
- API endpoint added if required
- Tests added
- Existing tests pass

## Constraints
- Use existing frontend layout
- Do not introduce a new UI library
- Do not change authentication flow

## Validation
- npm test passes
- npm run lint passes