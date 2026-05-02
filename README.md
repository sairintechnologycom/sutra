# Sutra CLI

**Sutra** is a governed AI development orchestrator that lets **Codex or Gemini plan**, **Claude Code execute**, and Sutra **validate, confirm, track, and report** the flow.

It is intentionally lightweight: a local Python CLI that can be installed directly into a repo and used from terminal.

---

## What Sutra does

- Validates that Codex/Gemini and Claude Code are available from the same developer shell.
- Confirms the chain: `Codex/Gemini -> Sutra -> Claude Code`.
- Converts a requirement file into bounded Claude Code tasks.
- Shows the tasks before execution.
- Requires confirmation before running tasks unless `--yes` / `--auto-approve` is used.
- Runs Claude Code task-by-task with timeout, max turns, model, budget, and allowed tools.
- Runs validation commands after every task.
- Tracks progress in `.sutra/runs/<RUN_ID>/progress.json` and `docs/progress.md`.
- Tracks Claude token usage or estimated usage in `.sutra/runs/<RUN_ID>/token-ledger.json`.
- Produces a run summary.

---

## Install

From this folder:

```bash
python -m pip install -e . --no-build-isolation
```

Verify:

```bash
sutra --help
```

---

## Prerequisites

Install and authenticate the CLIs you want to use:

```bash
claude --version
codex --version
# or
gemini --version
```

Sutra does not directly authenticate these tools. It validates that they are installed, callable, and usable in headless mode.

---

## First-time setup in a repo

```bash
sutra init
```

This creates:

```text
.sutra/config.json
CLAUDE.md
AGENTS.md
GEMINI.md
.claude/settings.json
.claude/skills/implement-task/SKILL.md
docs/progress.md
docs/decisions.md
requirements/REQ-001.md
```

---

## Confirm Codex/Gemini is connected to Claude through Sutra

For Codex:

```bash
sutra doctor --engine codex --smoke-test
```

For Gemini:

```bash
sutra doctor --engine gemini --smoke-test
```

Expected successful output includes:

```text
CHAIN CONFIRMED: codex -> Sutra -> Claude Code
```

or:

```text
CHAIN CONFIRMED: gemini -> Sutra -> Claude Code
```

This means Sutra can invoke the planner and Claude Code from the same environment.

---

## Normal developer flow

### 1. Write a requirement

Example:

```text
requirements/REQ-001.md
```

```markdown
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
```

### 2. Generate task plan

```bash
sutra plan --input requirements/REQ-001.md --engine codex
```

or:

```bash
sutra plan --input requirements/REQ-001.md --engine gemini
```

Sutra shows the generated tasks immediately.

### 3. Validate

```bash
sutra validate --run REQ-001
```

### 4. Approve

```bash
sutra approve --run REQ-001
```

### 5. Run

```bash
sutra run --run REQ-001 --smoke-test
```

Sutra shows each task before and during execution:

```text
▶ Executing T001: Inspect repository, confirm scope, and identify required files
  model=sonnet timeout=300s max_turns=2
  allowed_tools=Read, Bash(git status *), Bash(git diff *)
```

---

## Fully automated mode

```bash
sutra run \
  --input requirements/REQ-001.md \
  --engine codex \
  --auto-approve \
  --smoke-test
```

Use this only when `.claude/settings.json` and `.sutra/config.json` are properly locked down.

---

## Dry run

Dry run shows the planned task execution without invoking Claude Code:

```bash
sutra run --run REQ-001 --dry-run -y --skip-doctor
```

---

## Token report

```bash
sutra tokens report --run REQ-001
```

Important: actual token usage is used when Claude output exposes usage metadata. Otherwise, Sutra estimates token usage using prompt/output size and calculates savings against the configured baseline multiplier.

Config location:

```text
.sutra/config.json
```

Default baseline:

```json
{
  "policy": {
    "token_baseline_multiplier": 1.5
  }
}
```

Formula:

```text
tokens_saved = estimated_baseline_tokens - actual_or_estimated_tokens
```

---

## Main commands

```bash
sutra init
sutra doctor --engine codex --smoke-test
sutra plan --input requirements/REQ-001.md --engine codex
sutra validate --run REQ-001
sutra approve --run REQ-001
sutra run --run REQ-001
sutra run-task --run REQ-001 --task T002
sutra status --run REQ-001
sutra summarize --run REQ-001
sutra tokens report --run REQ-001
```

---

## Safety model

Sutra uses several controls:

- CLI chain validation before execution.
- Task plan validation.
- Human confirmation before execution by default.
- Claude timeout per task.
- Claude max-turns per task.
- Claude allowed tools per task.
- Validation command allow-list.
- Dangerous command deny-list.
- Progress and token ledger after each task.

---

## Current MVP limitations

- Token savings are estimates unless Claude Code emits usage details in structured output.
- Codex/Gemini planner output must be valid JSON; otherwise Sutra falls back to a local deterministic starter plan unless `--strict-planner` is used.
- Sutra validates the chain via Sutra-mediated execution, not a direct native integration between Codex/Gemini and Claude.
- The CLI is local-first and does not yet include a remote dashboard.

