from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

APP_DIR = ".sutra"
RUNS_DIR = "runs"
CONFIG_FILE = "config.json"

DEFAULT_CONFIG: Dict[str, Any] = {
    "default_engine": "codex",
    "claude": {
        "command": "claude",
        "default_model": "sonnet",
        "output_format": "json",
        "pass_budget_flag": True,
        "default_max_budget_usd": 0.50,
    },
    "planner": {
        "codex_command": "codex",
        "gemini_command": "gemini",
        "planner_timeout_seconds": 120,
        "allow_local_fallback": True,
    },
    "policy": {
        "require_confirmation_before_run": True,
        "max_timeout_seconds": 1800,
        "max_turns": 6,
        "validation_timeout_seconds": 300,
        "token_baseline_multiplier": 1.50,
        "allow_validation_command_prefixes": [
            "pytest",
            "python -m pytest",
            "npm test",
            "npm run test",
            "npm run lint",
            "pnpm test",
            "pnpm lint",
            "yarn test",
            "yarn lint",
            "git diff",
            "git status",
        ],
        "deny_command_patterns": [
            "rm -rf",
            "sudo",
            "curl .*\\| sh",
            "wget .*\\| sh",
            "terraform apply",
            "kubectl delete",
            "az .* delete",
            "aws .* delete",
        ],
    },
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def cwd() -> Path:
    return Path.cwd()


def sutra_dir() -> Path:
    return cwd() / APP_DIR


def config_path() -> Path:
    return sutra_dir() / CONFIG_FILE


def runs_dir() -> Path:
    return sutra_dir() / RUNS_DIR


def safe_print(message: str = "") -> None:
    print(message, flush=True)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def read_text(path: Path, default: str = "") -> str:
    return path.read_text(encoding="utf-8") if path.exists() else default


def slugify(value: str) -> str:
    value = value.strip().upper()
    value = re.sub(r"[^A-Z0-9]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value[:60] or "REQ"


def merge_dict(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config() -> Dict[str, Any]:
    existing = read_json(config_path(), {})
    return merge_dict(DEFAULT_CONFIG, existing or {})


def ensure_initialized() -> None:
    if not sutra_dir().exists():
        raise SystemExit("Sutra is not initialized in this repo. Run: sutra init")


def command_exists(command: str) -> bool:
    return shutil.which(command) is not None


def run_command(
    args: List[str],
    *,
    input_text: Optional[str] = None,
    timeout: int = 120,
    cwd_path: Optional[Path] = None,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        input=input_text,
        text=True,
        cwd=str(cwd_path or cwd()),
        capture_output=capture,
        timeout=timeout,
        check=False,
    )


def extract_json_blob(text: str) -> Optional[Any]:
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass

    # Some tools emit JSON event streams or prose around the final JSON.
    candidates: List[str] = []
    start_positions = [m.start() for m in re.finditer(r"[\[{]", text)]
    for start in start_positions:
        for end in range(len(text), start, -1):
            if text[end - 1] not in "}]":
                continue
            candidate = text[start:end]
            try:
                return json.loads(candidate)
            except Exception:
                continue
            finally:
                if len(candidates) > 1000:
                    break
    return None


def render_table(headers: List[str], rows: List[List[str]]) -> str:
    all_rows = [headers] + rows
    widths = [max(len(str(row[i])) for row in all_rows) for i in range(len(headers))]
    sep = "  ".join("-" * w for w in widths)
    lines = ["  ".join(str(headers[i]).ljust(widths[i]) for i in range(len(headers))), sep]
    for row in rows:
        lines.append("  ".join(str(row[i]).ljust(widths[i]) for i in range(len(headers))))
    return "\n".join(lines)


def show_tasks(plan: Dict[str, Any], title: str = "Tasks") -> None:
    tasks = plan.get("tasks", [])
    rows: List[List[str]] = []
    for task in tasks:
        rows.append([
            str(task.get("id", "")),
            str(task.get("status", "pending")),
            str(task.get("model", "")),
            str(task.get("timeout_seconds", "")),
            str(task.get("max_turns", "")),
            str(task.get("title", ""))[:80],
        ])
    safe_print(f"\n{title}")
    safe_print(render_table(["ID", "Status", "Model", "Timeout", "Turns", "Title"], rows) if rows else "No tasks found.")


def confirm(prompt: str, assume_yes: bool = False) -> bool:
    if assume_yes:
        return True
    value = input(f"{prompt} [y/N]: ").strip().lower()
    return value in {"y", "yes"}


def init_project(args: argparse.Namespace) -> None:
    sdir = sutra_dir()
    if sdir.exists() and not args.force:
        safe_print("Sutra already initialized. Use --force to refresh missing templates.")
    sdir.mkdir(parents=True, exist_ok=True)
    runs_dir().mkdir(parents=True, exist_ok=True)
    (sdir / "policies").mkdir(exist_ok=True)
    (sdir / "schemas").mkdir(exist_ok=True)

    if args.force or not config_path().exists():
        write_json(config_path(), DEFAULT_CONFIG)

    # Project guidance files.
    templates: Dict[Path, str] = {
        cwd() / "CLAUDE.md": """# Claude Code Project Rules\n\nYou are an execution worker controlled by Sutra.\n\n## Rules\n- Execute only the assigned Sutra task.\n- Do not expand scope.\n- Do not run destructive commands.\n- Keep changes scoped to the task.\n- Run required validation commands.\n- Return structured JSON when requested.\n\n## Progress\nAfter successful work, update `docs/progress.md` if the task asks for it.\n""",
        cwd() / "AGENTS.md": """# Sutra Planner Rules\n\nCodex acts as the planner/orchestrator.\n\nResponsibilities:\n1. Convert requirements into bounded Claude Code tasks.\n2. Include success criteria, timeout, max turns, validation commands, and allowed tools.\n3. Avoid long-running or broad tasks.\n4. Keep context small.\n""",
        cwd() / "GEMINI.md": """# Gemini Planner Rules\n\nGemini acts as the planner/orchestrator when selected.\n\nResponsibilities:\n1. Convert requirements into bounded Claude Code tasks.\n2. Include success criteria, timeout, max turns, validation commands, and allowed tools.\n3. Avoid long-running or broad tasks.\n4. Keep context small.\n""",
        cwd() / "docs" / "progress.md": "# Progress\n\nSutra will append task progress here.\n",
        cwd() / "docs" / "decisions.md": "# Decisions\n\nArchitecture and implementation decisions go here.\n",
        cwd() / "requirements" / "REQ-001.md": """# Requirement: Example\n\n## Goal\nDescribe the development requirement here.\n\n## Expected Outcome\n- Outcome 1\n- Outcome 2\n\n## Constraints\n- Keep implementation scoped.\n- Do not change unrelated files.\n\n## Validation\n- pytest passes or npm test passes, depending on project stack.\n""",
        cwd() / ".claude" / "settings.json": json.dumps({
            "permissions": {
                "allow": [
                    "Read",
                    "Edit",
                    "Bash(git status *)",
                    "Bash(git diff *)",
                    "Bash(pytest *)",
                    "Bash(npm test *)",
                    "Bash(npm run lint *)"
                ],
                "deny": [
                    "Bash(rm -rf *)",
                    "Bash(sudo *)",
                    "Bash(curl * | sh)",
                    "Bash(terraform apply *)",
                    "Bash(kubectl delete *)"
                ]
            }
        }, indent=2) + "\n",
        cwd() / ".claude" / "skills" / "implement-task" / "SKILL.md": """# Implement Sutra Task\n\nUse this skill when implementing a bounded Sutra task.\n\nSteps:\n1. Read the assigned task.\n2. Inspect only required context.\n3. Modify scoped files.\n4. Run validation commands.\n5. Summarize changed files, tests, risks, and next step.\n""",
    }

    for path, content in templates.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if args.force or not path.exists():
            path.write_text(content, encoding="utf-8")

    safe_print("Sutra initialized.")
    safe_print("Next: sutra doctor --engine codex --smoke-test")


def get_version(command: str) -> Tuple[bool, str]:
    if not command_exists(command):
        return False, "not found"
    for flag in ["--version", "version"]:
        try:
            cp = run_command([command, flag], timeout=15)
            out = (cp.stdout or cp.stderr or "").strip()
            if cp.returncode == 0 and out:
                return True, out.splitlines()[0]
        except Exception:
            continue
    return True, "found; version not reported"


def planner_smoke(engine: str, cfg: Dict[str, Any]) -> Tuple[bool, str]:
    timeout = int(cfg["planner"].get("planner_timeout_seconds", 120))
    prompt = "Return exactly this text and nothing else: SUTRA_PLANNER_OK"
    try:
        if engine == "codex":
            cmd = cfg["planner"].get("codex_command", "codex")
            cp = run_command([cmd, "exec", prompt], timeout=timeout)
        elif engine == "gemini":
            cmd = cfg["planner"].get("gemini_command", "gemini")
            cp = run_command([cmd, "-p", prompt], timeout=timeout)
        else:
            return False, f"unknown engine: {engine}"
    except Exception as exc:
        return False, str(exc)
    output = (cp.stdout or "") + (cp.stderr or "")
    return ("SUTRA_PLANNER_OK" in output and cp.returncode == 0), output.strip()[:500]


def claude_smoke(cfg: Dict[str, Any]) -> Tuple[bool, str]:
    prompt = "Return exactly this text and nothing else: SUTRA_CLAUDE_OK"
    cmd = cfg["claude"].get("command", "claude")
    try:
        cp = run_command([cmd, "-p", prompt], timeout=120)
    except Exception as exc:
        return False, str(exc)
    output = (cp.stdout or "") + (cp.stderr or "")
    return ("SUTRA_CLAUDE_OK" in output and cp.returncode == 0), output.strip()[:500]


def doctor(args: argparse.Namespace, *, quiet: bool = False) -> bool:
    ensure_initialized()
    cfg = load_config()
    engine = args.engine or cfg.get("default_engine", "codex")
    planner_cmd = cfg["planner"].get(f"{engine}_command", engine)
    claude_cmd = cfg["claude"].get("command", "claude")

    checks: List[Tuple[str, bool, str]] = []
    ok, msg = get_version(planner_cmd)
    checks.append((f"{engine} CLI", ok, msg))
    ok, msg = get_version(claude_cmd)
    checks.append(("Claude Code CLI", ok, msg))
    checks.append(("Sutra config", config_path().exists(), str(config_path())))
    checks.append(("CLAUDE.md", (cwd() / "CLAUDE.md").exists(), "project Claude rules"))
    checks.append((".claude/settings.json", (cwd() / ".claude" / "settings.json").exists(), "Claude permissions"))

    if args.smoke_test:
        ok, msg = planner_smoke(engine, cfg)
        checks.append((f"{engine} headless smoke test", ok, msg or "ok"))
        ok, msg = claude_smoke(cfg)
        checks.append(("Claude headless smoke test", ok, msg or "ok"))

    all_ok = all(item[1] for item in checks)
    if not quiet:
        rows = [[name, "PASS" if passed else "FAIL", detail] for name, passed, detail in checks]
        safe_print(render_table(["Check", "Status", "Detail"], rows))
        if all_ok:
            safe_print(f"\nCHAIN CONFIRMED: {engine} -> Sutra -> Claude Code")
        else:
            safe_print("\nChain not ready. Fix failed checks before running Sutra automation.")
    return all_ok


def build_planner_prompt(requirement: str, engine: str) -> str:
    return f"""
You are the Sutra planning engine using {engine}.

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
{{
  "title": "...",
  "risk": "low|medium|high",
  "tasks": [
    {{
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
    }}
  ]
}}

Requirement:
{requirement}
""".strip()


def run_planner(engine: str, prompt: str, cfg: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str, str]:
    timeout = int(cfg["planner"].get("planner_timeout_seconds", 120))
    try:
        if engine == "codex":
            cmd = cfg["planner"].get("codex_command", "codex")
            cp = run_command([cmd, "exec", prompt], timeout=timeout)
        elif engine == "gemini":
            cmd = cfg["planner"].get("gemini_command", "gemini")
            cp = run_command([cmd, "-p", prompt, "--output-format", "json"], timeout=timeout)
        else:
            return None, "", f"Unsupported engine: {engine}"
    except Exception as exc:
        return None, "", str(exc)

    raw = (cp.stdout or "") + "\n" + (cp.stderr or "")
    parsed = extract_json_blob(raw)

    # Gemini JSON output may wrap text in a response field.
    if isinstance(parsed, dict):
        for key in ["response", "text", "content", "message"]:
            value = parsed.get(key)
            if isinstance(value, str):
                nested = extract_json_blob(value)
                if isinstance(nested, dict):
                    parsed = nested
                    break

    if isinstance(parsed, dict) and isinstance(parsed.get("tasks"), list):
        return parsed, raw, ""
    return None, raw, "Planner output did not contain a valid Sutra task plan JSON."


def local_fallback_plan(requirement: str) -> Dict[str, Any]:
    title_line = next((line for line in requirement.splitlines() if line.strip().startswith("#")), "Requirement")
    title = title_line.lstrip("# ").strip() or "Requirement"
    return {
        "title": title,
        "risk": "medium",
        "planner_fallback": True,
        "tasks": [
            {
                "id": "T001",
                "title": "Inspect repository, confirm scope, and identify required files",
                "status": "pending",
                "agent": "claude-code",
                "model": "sonnet",
                "timeout_seconds": 300,
                "max_turns": 2,
                "max_budget_usd": 0.25,
                "allowed_tools": ["Read", "Bash(git status *)", "Bash(git diff *)"],
                "validation_commands": ["git status --short"],
                "success_criteria": [
                    "Repository structure inspected",
                    "Likely files and implementation approach identified",
                    "No unrelated files modified"
                ],
                "context_files": ["CLAUDE.md", "AGENTS.md", "docs/progress.md"]
            },
            {
                "id": "T002",
                "title": "Implement the smallest functional change for the requirement",
                "status": "pending",
                "agent": "claude-code",
                "model": "sonnet",
                "timeout_seconds": 900,
                "max_turns": 4,
                "max_budget_usd": 0.75,
                "allowed_tools": ["Read", "Edit", "Bash(git diff *)", "Bash(pytest *)", "Bash(npm test *)", "Bash(npm run lint *)"],
                "validation_commands": ["git diff --stat"],
                "success_criteria": [
                    "Scoped implementation completed",
                    "No unrelated files modified",
                    "Validation commands executed where applicable"
                ],
                "context_files": ["CLAUDE.md", "docs/progress.md", "docs/decisions.md"]
            },
            {
                "id": "T003",
                "title": "Run validation and fix scoped failures",
                "status": "pending",
                "agent": "claude-code",
                "model": "sonnet",
                "timeout_seconds": 900,
                "max_turns": 3,
                "max_budget_usd": 0.50,
                "allowed_tools": ["Read", "Edit", "Bash(git diff *)", "Bash(pytest *)", "Bash(npm test *)", "Bash(npm run lint *)"],
                "validation_commands": ["git diff --stat"],
                "success_criteria": [
                    "Validation results captured",
                    "Obvious scoped failures fixed",
                    "Remaining risks documented"
                ],
                "context_files": ["CLAUDE.md", "docs/progress.md"]
            },
            {
                "id": "T004",
                "title": "Update progress and summarize evidence",
                "status": "pending",
                "agent": "claude-code",
                "model": "sonnet",
                "timeout_seconds": 300,
                "max_turns": 2,
                "max_budget_usd": 0.25,
                "allowed_tools": ["Read", "Edit", "Bash(git diff *)", "Bash(git status *)"],
                "validation_commands": ["git status --short", "git diff --stat"],
                "success_criteria": [
                    "docs/progress.md updated",
                    "Evidence summary captured",
                    "Final git diff summarized"
                ],
                "context_files": ["CLAUDE.md", "docs/progress.md", "docs/decisions.md"]
            },
        ],
    }


def normalize_plan(plan: Dict[str, Any], run_id: str, requirement_file: str, engine: str, requirement: str) -> Dict[str, Any]:
    tasks = plan.get("tasks", [])
    for i, task in enumerate(tasks, start=1):
        task.setdefault("id", f"T{i:03d}")
        task.setdefault("status", "pending")
        task.setdefault("agent", "claude-code")
        task.setdefault("model", "sonnet")
        task.setdefault("timeout_seconds", 900)
        task.setdefault("max_turns", 4)
        task.setdefault("max_budget_usd", 0.50)
        task.setdefault("allowed_tools", ["Read", "Edit", "Bash(git diff *)"])
        task.setdefault("validation_commands", ["git diff --stat"])
        task.setdefault("success_criteria", ["Task completed and summarized"])
        task.setdefault("context_files", ["CLAUDE.md", "docs/progress.md"])
    return {
        "run_id": run_id,
        "title": plan.get("title", run_id),
        "engine": engine,
        "requirement_file": requirement_file,
        "requirement_excerpt": requirement[:4000],
        "risk": plan.get("risk", "medium"),
        "status": "planned",
        "approved": False,
        "created_at": now_iso(),
        "planner_fallback": bool(plan.get("planner_fallback")),
        "tasks": tasks,
    }


def plan_command(args: argparse.Namespace) -> None:
    ensure_initialized()
    cfg = load_config()
    engine = args.engine or cfg.get("default_engine", "codex")
    req_path = Path(args.input)
    if not req_path.exists():
        raise SystemExit(f"Requirement file not found: {req_path}")
    requirement = req_path.read_text(encoding="utf-8")
    run_id = args.run_id or slugify(req_path.stem if req_path.stem else "REQ")
    run_path = runs_dir() / run_id
    run_path.mkdir(parents=True, exist_ok=True)

    prompt = build_planner_prompt(requirement, engine)
    (run_path / "planner-prompt.md").write_text(prompt, encoding="utf-8")

    safe_print(f"Planning with {engine}...")
    parsed, raw, error = run_planner(engine, prompt, cfg)
    (run_path / "planner-output.raw.txt").write_text(raw or error, encoding="utf-8")

    if parsed is None:
        if args.strict_planner or not cfg["planner"].get("allow_local_fallback", True):
            raise SystemExit(f"Planner failed: {error}\nRaw output saved to {run_path / 'planner-output.raw.txt'}")
        safe_print("Planner did not return valid task JSON. Using Sutra local fallback planner.")
        parsed = local_fallback_plan(requirement)

    plan = normalize_plan(parsed, run_id, str(req_path), engine, requirement)
    write_json(run_path / "task-plan.json", plan)
    write_json(run_path / "progress.json", {"run_id": run_id, "events": [], "updated_at": now_iso()})
    write_json(run_path / "token-ledger.json", {"run_id": run_id, "tasks": [], "updated_at": now_iso()})

    show_tasks(plan, title=f"Generated Sutra task plan: {run_id}")
    safe_print(f"\nPlan saved: {run_path / 'task-plan.json'}")
    if plan.get("planner_fallback"):
        safe_print("Note: local fallback planner was used. Run with --strict-planner to require Codex/Gemini JSON output.")
    safe_print("Next: sutra validate --run " + run_id)


def load_plan(run_id: str) -> Tuple[Path, Dict[str, Any]]:
    ensure_initialized()
    run_path = runs_dir() / run_id
    plan_file = run_path / "task-plan.json"
    if not plan_file.exists():
        raise SystemExit(f"Task plan not found: {plan_file}")
    return run_path, read_json(plan_file, {})


def validate_task(task: Dict[str, Any], cfg: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    required = ["id", "title", "status", "agent", "model", "timeout_seconds", "max_turns", "allowed_tools", "validation_commands", "success_criteria"]
    for key in required:
        if key not in task:
            errors.append(f"{task.get('id', '<unknown>')}: missing {key}")
    max_timeout = int(cfg["policy"].get("max_timeout_seconds", 1800))
    max_turns = int(cfg["policy"].get("max_turns", 6))
    try:
        if int(task.get("timeout_seconds", 0)) <= 0 or int(task.get("timeout_seconds", 0)) > max_timeout:
            errors.append(f"{task.get('id')}: timeout_seconds must be 1..{max_timeout}")
    except Exception:
        errors.append(f"{task.get('id')}: timeout_seconds must be integer")
    try:
        if int(task.get("max_turns", 0)) <= 0 or int(task.get("max_turns", 0)) > max_turns:
            errors.append(f"{task.get('id')}: max_turns must be 1..{max_turns}")
    except Exception:
        errors.append(f"{task.get('id')}: max_turns must be integer")
    if not isinstance(task.get("success_criteria"), list) or not task.get("success_criteria"):
        errors.append(f"{task.get('id')}: success_criteria must be non-empty list")
    if not isinstance(task.get("allowed_tools"), list) or not task.get("allowed_tools"):
        errors.append(f"{task.get('id')}: allowed_tools must be non-empty list")
    if not isinstance(task.get("validation_commands"), list):
        errors.append(f"{task.get('id')}: validation_commands must be a list")
    for tool in task.get("allowed_tools", []):
        for pattern in cfg["policy"].get("deny_command_patterns", []):
            if re.search(pattern, tool, flags=re.IGNORECASE):
                errors.append(f"{task.get('id')}: allowed tool matches denied pattern: {tool}")
    return errors


def validation_command_allowed(command: str, cfg: Dict[str, Any]) -> bool:
    for pattern in cfg["policy"].get("deny_command_patterns", []):
        if re.search(pattern, command, flags=re.IGNORECASE):
            return False
    prefixes = cfg["policy"].get("allow_validation_command_prefixes", [])
    return any(command.strip().startswith(prefix) for prefix in prefixes)


def validate_command(args: argparse.Namespace) -> None:
    run_path, plan = load_plan(args.run)
    cfg = load_config()
    errors: List[str] = []

    for path in ["CLAUDE.md", ".claude/settings.json"]:
        if not (cwd() / path).exists():
            errors.append(f"Missing required file: {path}")

    settings_file = cwd() / ".claude" / "settings.json"
    if settings_file.exists():
        try:
            json.loads(settings_file.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(f"Invalid .claude/settings.json: {exc}")

    for task in plan.get("tasks", []):
        errors.extend(validate_task(task, cfg))
        for command in task.get("validation_commands", []):
            if not validation_command_allowed(command, cfg):
                errors.append(f"{task.get('id')}: validation command is not allow-listed: {command}")

    if errors:
        safe_print("Validation failed:")
        for error in errors:
            safe_print(f"- {error}")
        raise SystemExit(1)

    safe_print(f"Validation passed for run {args.run}.")
    show_tasks(plan, title="Validated tasks")


def approve_command(args: argparse.Namespace) -> None:
    run_path, plan = load_plan(args.run)
    plan["approved"] = True
    plan["approved_at"] = now_iso()
    write_json(run_path / "task-plan.json", plan)
    safe_print(f"Approved run {args.run}.")


def build_claude_prompt(plan: Dict[str, Any], task: Dict[str, Any]) -> str:
    context_files = "\n".join(f"- {p}" for p in task.get("context_files", []))
    criteria = "\n".join(f"- {c}" for c in task.get("success_criteria", []))
    validation = "\n".join(f"- {c}" for c in task.get("validation_commands", []))
    requirement = plan.get("requirement_excerpt", "")
    return f"""
# Sutra Claude Code Task

## Run
{plan.get('run_id')}: {plan.get('title')}

## Task
ID: {task.get('id')}\nTitle: {task.get('title')}

## Operating Rules
- Execute only this task.
- Do not expand scope.
- Do not run destructive commands.
- Keep changes small and task-scoped.
- Prefer reading listed context files before modifying code.
- Run the validation commands if applicable.
- Update docs/progress.md only if the task asks for progress/documentation update.

## Requirement Excerpt
{requirement}

## Context Files
{context_files}

## Success Criteria
{criteria}

## Validation Commands
{validation}

## Required Response
Return structured JSON with this shape:
{{
  "task_id": "{task.get('id')}",
  "status": "completed|failed",
  "summary": "...",
  "files_changed": [],
  "tests_run": [],
  "risks": [],
  "next_recommendation": "..."
}}
""".strip()


def claude_command_for_task(task: Dict[str, Any], prompt: str, cfg: Dict[str, Any]) -> List[str]:
    claude_cfg = cfg["claude"]
    cmd = [claude_cfg.get("command", "claude"), "-p", prompt]
    model = task.get("model") or claude_cfg.get("default_model")
    if model:
        cmd += ["--model", str(model)]
    if task.get("max_turns"):
        cmd += ["--max-turns", str(task.get("max_turns"))]
    if claude_cfg.get("output_format"):
        cmd += ["--output-format", str(claude_cfg.get("output_format"))]
    if claude_cfg.get("pass_budget_flag", True) and task.get("max_budget_usd") is not None:
        cmd += ["--max-budget-usd", str(task.get("max_budget_usd"))]
    allowed = task.get("allowed_tools") or []
    if allowed:
        cmd += ["--allowedTools"] + [str(item) for item in allowed]
    return cmd


def append_progress_event(run_path: Path, event: Dict[str, Any]) -> None:
    progress_file = run_path / "progress.json"
    progress = read_json(progress_file, {"events": []})
    progress.setdefault("events", []).append(event)
    progress["updated_at"] = now_iso()
    write_json(progress_file, progress)


def append_docs_progress(plan: Dict[str, Any], task: Dict[str, Any], status: str, summary: str) -> None:
    progress_md = cwd() / "docs" / "progress.md"
    progress_md.parent.mkdir(parents=True, exist_ok=True)
    line = f"\n## {now_iso()} — {plan.get('run_id')} / {task.get('id')} / {status}\n\n{summary}\n"
    with progress_md.open("a", encoding="utf-8") as fh:
        fh.write(line)


def estimate_tokens(text: str) -> int:
    # Conservative rough estimate: 1 token ≈ 4 chars for English/code-ish text.
    return max(1, int(len(text) / 4))


def find_usage(obj: Any) -> Dict[str, float]:
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "estimated_cost_usd": 0.0,
    }

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                lk = k.lower()
                if isinstance(v, (int, float)):
                    if lk in {"input_tokens", "prompt_tokens"}:
                        usage["input_tokens"] += float(v)
                    elif lk in {"output_tokens", "completion_tokens"}:
                        usage["output_tokens"] += float(v)
                    elif "cache_read" in lk and "token" in lk:
                        usage["cache_read_tokens"] += float(v)
                    elif ("cache_creation" in lk or "cache_write" in lk) and "token" in lk:
                        usage["cache_write_tokens"] += float(v)
                    elif lk in {"cost_usd", "estimated_cost_usd", "total_cost_usd"}:
                        usage["estimated_cost_usd"] += float(v)
                else:
                    walk(v)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(obj)
    return usage


def update_token_ledger(run_path: Path, task: Dict[str, Any], prompt: str, output: str, cfg: Dict[str, Any], parsed_output: Any) -> None:
    ledger_file = run_path / "token-ledger.json"
    ledger = read_json(ledger_file, {"tasks": []})
    usage = find_usage(parsed_output) if parsed_output is not None else find_usage(extract_json_blob(output) or {})
    estimated = False
    actual_total = int(usage["input_tokens"] + usage["output_tokens"])
    if actual_total <= 1:
        usage["input_tokens"] = estimate_tokens(prompt)
        usage["output_tokens"] = estimate_tokens(output)
        actual_total = int(usage["input_tokens"] + usage["output_tokens"])
        estimated = True

    multiplier = float(cfg["policy"].get("token_baseline_multiplier", 1.50))
    baseline_tokens = int(actual_total * multiplier)
    saved = max(0, baseline_tokens - actual_total)
    ledger.setdefault("tasks", []).append({
        "task_id": task.get("id"),
        "model": task.get("model"),
        "actual": {
            "input_tokens": int(usage["input_tokens"]),
            "output_tokens": int(usage["output_tokens"]),
            "cache_read_tokens": int(usage["cache_read_tokens"]),
            "cache_write_tokens": int(usage["cache_write_tokens"]),
            "estimated_cost_usd": round(float(usage["estimated_cost_usd"]), 6),
            "usage_estimated_by_sutra": estimated,
        },
        "baseline": {
            "method": "multiplier",
            "multiplier": multiplier,
            "estimated_tokens": baseline_tokens,
        },
        "savings": {
            "tokens_saved": saved,
            "percent_saved": round((saved / baseline_tokens) * 100, 2) if baseline_tokens else 0,
        },
        "saving_reasons": [
            "bounded_task_execution",
            "limited_context_prompt",
            "max_turns_cap",
            "task_specific_allowed_tools",
        ],
        "created_at": now_iso(),
    })
    ledger["updated_at"] = now_iso()
    write_json(ledger_file, ledger)


def run_validation_commands(task: Dict[str, Any], cfg: Dict[str, Any], run_path: Path) -> Tuple[bool, List[Dict[str, Any]]]:
    results: List[Dict[str, Any]] = []
    timeout = int(cfg["policy"].get("validation_timeout_seconds", 300))
    for command in task.get("validation_commands", []):
        if not validation_command_allowed(command, cfg):
            results.append({"command": command, "status": "blocked", "output": "Command not allow-listed"})
            return False, results
        safe_print(f"  validation> {command}")
        try:
            cp = run_command(shlex.split(command), timeout=timeout)
            output = ((cp.stdout or "") + (cp.stderr or "")).strip()
            results.append({"command": command, "status": "passed" if cp.returncode == 0 else "failed", "returncode": cp.returncode, "output": output[-4000:]})
        except Exception as exc:
            results.append({"command": command, "status": "failed", "output": str(exc)})
            return False, results
    return all(r.get("status") == "passed" for r in results), results


def parse_task_status(output: str) -> str:
    parsed = extract_json_blob(output)
    if isinstance(parsed, dict):
        status = str(parsed.get("status", "")).lower()
        if status in {"completed", "failed"}:
            return status
    lowered = output.lower()
    if '"status"' in lowered and "completed" in lowered:
        return "completed"
    return "completed" if output.strip() else "failed"


def run_task_internal(run_path: Path, plan: Dict[str, Any], task: Dict[str, Any], cfg: Dict[str, Any], dry_run: bool = False) -> str:
    prompt = build_claude_prompt(plan, task)
    task_dir = run_path / "tasks" / str(task.get("id"))
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "prompt.md").write_text(prompt, encoding="utf-8")

    safe_print(f"\n▶ Executing {task.get('id')}: {task.get('title')}")
    safe_print(f"  model={task.get('model')} timeout={task.get('timeout_seconds')}s max_turns={task.get('max_turns')}")
    safe_print("  allowed_tools=" + ", ".join(task.get("allowed_tools", [])))

    if dry_run:
        safe_print("  DRY RUN: Claude Code invocation skipped.")
        task["status"] = "dry-run"
        append_progress_event(run_path, {"task_id": task.get("id"), "status": "dry-run", "at": now_iso()})
        return "dry-run"

    cmd = claude_command_for_task(task, prompt, cfg)
    safe_cmd = " ".join(shlex.quote(part if len(part) < 120 else part[:120] + "...") for part in cmd[:])
    (task_dir / "command.txt").write_text(safe_cmd + "\n", encoding="utf-8")

    try:
        cp = run_command(cmd, timeout=int(task.get("timeout_seconds", 900)))
        output = ((cp.stdout or "") + "\n" + (cp.stderr or "")).strip()
        (task_dir / "claude-output.txt").write_text(output + "\n", encoding="utf-8")
        parsed = extract_json_blob(output)
        write_json(task_dir / "claude-output.parsed.json", parsed if parsed is not None else {"raw_output": output[-4000:]})
        update_token_ledger(run_path, task, prompt, output, cfg, parsed)

        status = parse_task_status(output)
        if cp.returncode != 0:
            status = "failed"
            safe_print(f"  Claude returned non-zero exit code: {cp.returncode}")

        safe_print("  running validation commands...")
        validation_ok, validation_results = run_validation_commands(task, cfg, run_path)
        write_json(task_dir / "validation-results.json", validation_results)
        if not validation_ok:
            status = "failed"

        summary = output[-1000:] if output else "No Claude output captured."
        task["status"] = status
        task["completed_at" if status == "completed" else "updated_at"] = now_iso()
        append_progress_event(run_path, {"task_id": task.get("id"), "status": status, "at": now_iso(), "validation_ok": validation_ok})
        append_docs_progress(plan, task, status, summary)
        safe_print(f"  result={status}")
        return status
    except subprocess.TimeoutExpired:
        task["status"] = "timeout"
        append_progress_event(run_path, {"task_id": task.get("id"), "status": "timeout", "at": now_iso()})
        safe_print("  result=timeout")
        return "timeout"
    except Exception as exc:
        task["status"] = "failed"
        append_progress_event(run_path, {"task_id": task.get("id"), "status": "failed", "at": now_iso(), "error": str(exc)})
        safe_print(f"  result=failed: {exc}")
        return "failed"


def run_task_command(args: argparse.Namespace) -> None:
    run_path, plan = load_plan(args.run)
    cfg = load_config()
    task = next((t for t in plan.get("tasks", []) if t.get("id") == args.task), None)
    if task is None:
        raise SystemExit(f"Task not found: {args.task}")
    status = run_task_internal(run_path, plan, task, cfg, dry_run=args.dry_run)
    write_json(run_path / "task-plan.json", plan)
    if status not in {"completed", "dry-run"}:
        raise SystemExit(1)


def run_command_main(args: argparse.Namespace) -> None:
    if args.input:
        plan_args = argparse.Namespace(input=args.input, engine=args.engine, run_id=args.run, strict_planner=args.strict_planner)
        plan_command(plan_args)
        run_id = args.run or slugify(Path(args.input).stem)
    else:
        run_id = args.run
    if not run_id:
        raise SystemExit("Provide --run or --input")

    run_path, plan = load_plan(run_id)
    cfg = load_config()

    if not args.skip_doctor:
        doctor_args = argparse.Namespace(engine=plan.get("engine"), smoke_test=args.smoke_test)
        if not doctor(doctor_args, quiet=False):
            raise SystemExit(1)

    # Always validate before run.
    validate_args = argparse.Namespace(run=run_id)
    validate_command(validate_args)

    show_tasks(plan, title=f"Tasks to execute for run {run_id}")
    requires_confirmation = cfg["policy"].get("require_confirmation_before_run", True)
    if requires_confirmation and not confirm("Proceed with executing these Claude Code tasks?", assume_yes=args.yes or args.auto_approve):
        safe_print("Execution cancelled.")
        return
    plan["approved"] = True
    plan["status"] = "running"
    plan["started_at"] = plan.get("started_at") or now_iso()
    write_json(run_path / "task-plan.json", plan)

    for task in plan.get("tasks", []):
        if task.get("status") == "completed" and not args.rerun_completed:
            safe_print(f"Skipping completed task {task.get('id')}")
            continue
        status = run_task_internal(run_path, plan, task, cfg, dry_run=args.dry_run)
        write_json(run_path / "task-plan.json", plan)
        if status != "completed" and not args.dry_run:
            plan["status"] = "blocked"
            write_json(run_path / "task-plan.json", plan)
            safe_print(f"Run blocked at task {task.get('id')}. Review {run_path / 'tasks' / str(task.get('id'))}")
            raise SystemExit(1)

    plan["status"] = "completed" if not args.dry_run else "dry-run"
    plan["completed_at"] = now_iso()
    write_json(run_path / "task-plan.json", plan)
    safe_print(f"\nRun {run_id} finished with status: {plan['status']}")
    summarize_run(run_id)


def status_command(args: argparse.Namespace) -> None:
    _, plan = load_plan(args.run)
    safe_print(f"Run: {plan.get('run_id')} | status={plan.get('status')} | engine={plan.get('engine')} | approved={plan.get('approved')}")
    show_tasks(plan, title="Current task status")


def summarize_run(run_id: str) -> None:
    run_path, plan = load_plan(run_id)
    ledger = read_json(run_path / "token-ledger.json", {"tasks": []})
    rows = []
    for t in plan.get("tasks", []):
        rows.append(f"- {t.get('id')}: {t.get('status')} — {t.get('title')}")
    total_actual = 0
    total_baseline = 0
    total_saved = 0
    for item in ledger.get("tasks", []):
        actual = item.get("actual", {})
        total_actual += int(actual.get("input_tokens", 0)) + int(actual.get("output_tokens", 0))
        total_baseline += int(item.get("baseline", {}).get("estimated_tokens", 0))
        total_saved += int(item.get("savings", {}).get("tokens_saved", 0))

    md = f"""# Sutra Run Summary: {run_id}

## Status
{plan.get('status')}

## Tasks
{chr(10).join(rows)}

## Token Ledger
- Actual tokens: {total_actual}
- Estimated baseline tokens: {total_baseline}
- Estimated tokens saved: {total_saved}
- Saving percentage: {round((total_saved / total_baseline) * 100, 2) if total_baseline else 0}%

## Artifacts
- Task plan: `{run_path / 'task-plan.json'}`
- Progress: `{run_path / 'progress.json'}`
- Token ledger: `{run_path / 'token-ledger.json'}`
"""
    (run_path / "summary.md").write_text(md, encoding="utf-8")
    safe_print(md)


def summarize_command(args: argparse.Namespace) -> None:
    summarize_run(args.run)


def tokens_report_command(args: argparse.Namespace) -> None:
    run_path, _ = load_plan(args.run)
    ledger = read_json(run_path / "token-ledger.json", {"tasks": []})
    rows: List[List[str]] = []
    total_actual = total_baseline = total_saved = 0
    for item in ledger.get("tasks", []):
        actual_obj = item.get("actual", {})
        actual = int(actual_obj.get("input_tokens", 0)) + int(actual_obj.get("output_tokens", 0))
        baseline = int(item.get("baseline", {}).get("estimated_tokens", 0))
        saved = int(item.get("savings", {}).get("tokens_saved", 0))
        total_actual += actual
        total_baseline += baseline
        total_saved += saved
        rows.append([
            str(item.get("task_id")),
            str(item.get("model")),
            str(actual),
            str(baseline),
            str(saved),
            str(item.get("savings", {}).get("percent_saved", 0)) + "%",
            "yes" if actual_obj.get("usage_estimated_by_sutra") else "no",
        ])
    safe_print(render_table(["Task", "Model", "Actual", "Baseline", "Saved", "%", "Estimated"], rows) if rows else "No token ledger entries yet.")
    safe_print(f"\nTotal actual tokens: {total_actual}")
    safe_print(f"Estimated baseline tokens: {total_baseline}")
    safe_print(f"Estimated tokens saved: {total_saved}")
    safe_print(f"Estimated saving: {round((total_saved / total_baseline) * 100, 2) if total_baseline else 0}%")
    safe_print("\nNote: Tokens saved are estimated against Sutra's configured baseline multiplier unless Claude usage data is available in output.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sutra", description="Sutra: governed AI coding flow for Codex/Gemini and Claude Code")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="Initialize Sutra files in the current repo")
    p.add_argument("--force", action="store_true", help="Refresh templates/config")
    p.set_defaults(func=init_project)

    p = sub.add_parser("doctor", help="Validate Codex/Gemini and Claude Code chain")
    p.add_argument("--engine", choices=["codex", "gemini"], default=None)
    p.add_argument("--smoke-test", action="store_true", help="Run headless smoke tests for planner and Claude")
    p.set_defaults(func=doctor)

    p = sub.add_parser("plan", help="Generate bounded Claude Code task plan")
    p.add_argument("--input", required=True, help="Requirement markdown file")
    p.add_argument("--engine", choices=["codex", "gemini"], default=None)
    p.add_argument("--run-id", default=None)
    p.add_argument("--strict-planner", action="store_true", help="Fail if planner does not return valid JSON")
    p.set_defaults(func=plan_command)

    p = sub.add_parser("validate", help="Validate Sutra run plan and Claude config")
    p.add_argument("--run", required=True)
    p.set_defaults(func=validate_command)

    p = sub.add_parser("approve", help="Approve a planned run")
    p.add_argument("--run", required=True)
    p.set_defaults(func=approve_command)

    p = sub.add_parser("run-task", help="Run one task through Claude Code")
    p.add_argument("--run", required=True)
    p.add_argument("--task", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=run_task_command)

    p = sub.add_parser("run", help="Run all pending tasks through Claude Code")
    p.add_argument("--run", default=None)
    p.add_argument("--input", default=None, help="Optional requirement file; plans then runs")
    p.add_argument("--engine", choices=["codex", "gemini"], default=None)
    p.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")
    p.add_argument("--auto-approve", action="store_true", help="Skip confirmation and mark run approved")
    p.add_argument("--skip-doctor", action="store_true")
    p.add_argument("--smoke-test", action="store_true", help="Run headless smoke tests before execution")
    p.add_argument("--strict-planner", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--rerun-completed", action="store_true")
    p.set_defaults(func=run_command_main)

    p = sub.add_parser("status", help="Show run status and tasks")
    p.add_argument("--run", required=True)
    p.set_defaults(func=status_command)

    p = sub.add_parser("summarize", help="Generate run summary")
    p.add_argument("--run", required=True)
    p.set_defaults(func=summarize_command)

    p = sub.add_parser("tokens", help="Token ledger commands")
    token_sub = p.add_subparsers(dest="tokens_command", required=True)
    tr = token_sub.add_parser("report", help="Show token saving report")
    tr.add_argument("--run", required=True)
    tr.set_defaults(func=tokens_report_command)

    return parser


def main(argv: Optional[List[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except KeyboardInterrupt:
        safe_print("Interrupted.")
        raise SystemExit(130)


if __name__ == "__main__":
    main()
