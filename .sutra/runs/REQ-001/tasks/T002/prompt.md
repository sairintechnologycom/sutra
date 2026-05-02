# Sutra Claude Code Task

## Run
REQ-001: Add Student Progress Dashboard

## Task
ID: T002
Title: Implement API endpoint for student progress data

## Operating Rules
- Execute only this task.
- Do not expand scope.
- Do not run destructive commands.
- Keep changes small and task-scoped.
- Prefer reading listed context files before modifying code.
- Run the validation commands if applicable.
- Update docs/progress.md only if the task asks for progress/documentation update.

## Requirement Excerpt
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


## Context Files
- CLAUDE.md

## Success Criteria
- API endpoint serves progress data including subject, topic, quiz scores, and mastery levels.
- Endpoint follows existing architectural patterns.
- Unit tests for the API endpoint are passing.

## Validation Commands
- npm test

## Required Response
Return structured JSON with this shape:
{
  "task_id": "T002",
  "status": "completed|failed",
  "summary": "...",
  "files_changed": [],
  "tests_run": [],
  "risks": [],
  "next_recommendation": "..."
}