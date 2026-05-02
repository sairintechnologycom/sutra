from __future__ import annotations

import argparse
import difflib
import glob
import http.server
import json
import os
import re
import shlex
import shutil
import socketserver
import subprocess
import sys
import textwrap
import threading
import webbrowser
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
    "git": {
        "auto_branch": True,
        "auto_commit": True,
        "branch_prefix": "sutra/",
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
            "test ",
            "grep ",
            "ls ",
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


def summarize_text(text: str) -> str:
    lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    chars = len(text)
    kb = chars / 1024
    return f"{lines} lines ({kb:.1f}KB)"


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


def is_git_repo() -> bool:
    try:
        cp = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True, check=False)
        return cp.returncode == 0
    except Exception:
        return False


def run_command(
    args: List[str],
    *,
    input_text: Optional[str] = None,
    timeout: int = 120,
    cwd_path: Optional[Path] = None,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    # Use DEVNULL for stdin if no input_text is provided to prevent hanging on interactive prompts
    stdin = subprocess.PIPE if input_text else subprocess.DEVNULL
    return subprocess.run(
        args,
        input=input_text,
        text=True,
        cwd=str(cwd_path or cwd()),
        capture_output=capture,
        timeout=timeout,
        check=False,
        stdin=stdin
    )


def extract_json_blob(text: str) -> Optional[Any]:
    text = text.strip()
    if not text:
        return None
    
    # Try direct parse.
    try:
        return json.loads(text)
    except Exception:
        pass

    # Look for JSON blocks ```json ... ```
    match = re.search(r"```json\s+(.*?)\s+```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except Exception:
            pass

    # Some tools emit JSON event streams or prose around the final JSON.
    # Look for the first { or [ and the last } or ].
    start = text.find("{")
    if start == -1:
        start = text.find("[")
    
    end = text.rfind("}")
    if end == -1:
        end = text.rfind("]")
    
    if start != -1 and end != -1 and end > start:
        candidate = text[start : end + 1]
        try:
            return json.loads(candidate)
        except Exception:
            pass

    return None


def validate_plan_schema(plan: Any) -> Tuple[bool, str]:
    if not isinstance(plan, dict):
        return False, "Plan is not a dictionary"
    if "tasks" not in plan or not isinstance(plan["tasks"], list):
        return False, "Plan missing 'tasks' list"
    
    for i, task in enumerate(plan["tasks"]):
        if not isinstance(task, dict):
            return False, f"Task {i} is not a dictionary"
        # Minimum required fields from planner.
        for field in ["title"]:
            if field not in task:
                return False, f"Task {i} missing required field: {field}"
    
    return True, ""


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


def interactive_edit_plan(plan: Dict[str, Any]) -> None:
    while True:
        show_tasks(plan, title="Plan Review (Interactive Mode)")
        safe_print("\nOptions: [e]dit task, [d]elete task, [a]dd task, [f]inish")
        choice = input("Choice: ").strip().lower()
        if choice == "f":
            break
        elif choice == "e":
            tid = input("Task ID to edit: ").strip().upper()
            task = next((t for t in plan["tasks"] if t["id"] == tid), None)
            if not task:
                safe_print(f"Task {tid} not found.")
                continue
            safe_print(f"Editing {tid}: {task['title']}")
            task["title"] = input(f"New title [{task['title']}]: ").strip() or task["title"]
            task["model"] = input(f"New model [{task['model']}]: ").strip() or task["model"]
            try:
                task["timeout_seconds"] = int(input(f"New timeout [{task['timeout_seconds']}]: ").strip() or task["timeout_seconds"])
                task["max_turns"] = int(input(f"New max turns [{task['max_turns']}]: ").strip() or task["max_turns"])
            except ValueError:
                safe_print("Invalid number. Keeping old values.")
        elif choice == "d":
            tid = input("Task ID to delete: ").strip().upper()
            plan["tasks"] = [t for t in plan["tasks"] if t["id"] != tid]
        elif choice == "a":
            new_id = f"T{len(plan['tasks']) + 1:03d}"
            title = input("Task title: ").strip()
            if not title:
                continue
            plan["tasks"].append({
                "id": new_id,
                "title": title,
                "status": "pending",
                "agent": "claude-code",
                "model": "sonnet",
                "timeout_seconds": 900,
                "max_turns": 4,
            })
        else:
            safe_print("Unknown choice.")


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
            safe_print("Next: sutra plan --input requirements/REQ-001.md")
        else:
            safe_print("\nChain not ready. Fix failed checks before running Sutra automation.")
    return all_ok


def get_repo_map() -> str:
    if not is_git_repo():
        try:
            files = []
            for root, dirs, filenames in os.walk(".", topdown=True):
                dirs[:] = [d for d in dirs if d not in {".git", ".sutra", "__pycache__", "node_modules", ".venv", ".pytest_cache"}]
                for f in filenames:
                    files.append(os.path.join(root, f).lstrip("./"))
            return "\n".join(files[:1000])
        except Exception:
            return "No repo map available."
    try:
        cp = run_command(["git", "ls-files"], timeout=15)
        if cp.returncode == 0:
            return cp.stdout.strip()
    except Exception:
        pass
    return "No repo map available."
def detect_project_type() -> Dict[str, Any]:
    info = {
        "type": "unknown",
        "suggested_validation": ["git status --short"]
    }
    if (cwd() / "package.json").exists():
        info["type"] = "nodejs"
        info["suggested_validation"].append("npm test")
    if (cwd() / "pytest.ini").exists() or (cwd() / "conftest.py").exists() or glob.glob("**/test_*.py", recursive=True):
        info["type"] = "python"
        info["suggested_validation"].append("pytest")
    if (cwd() / "Cargo.toml").exists():
        info["type"] = "rust"
        info["suggested_validation"].append("cargo test")
    if (cwd() / "go.mod").exists():
        info["type"] = "go"
        info["suggested_validation"].append("go test ./...")
    return info


def build_planner_prompt(requirement: str, engine: str, repo_map: Optional[str] = None) -> str:
    project_info = detect_project_type()
    project_context = f"\nProject Type: {project_info['type']}\nSuggested Validation: {', '.join(project_info['suggested_validation'])}\n"
    repo_context = f"\nRepository Map:\n{repo_map}\n" if repo_map else ""
    return f"""
You are the Sutra planning engine using {engine}.

Convert the requirement into a bounded task plan for Claude Code. Return ONLY valid JSON.
{project_context}
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
{repo_context}
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

    if parsed:
        ok, err = validate_plan_schema(parsed)
        if ok:
            return parsed, raw, ""
        return None, raw, f"Planner output failed schema validation: {err}"
    
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
    
    if args.input == "-":
        safe_print("Reading requirement from stdin (paste text then press Ctrl+D)...")
        requirement = sys.stdin.read()
        req_name = "STDIN"
    else:
        req_path = Path(args.input)
        if not req_path.exists():
            raise SystemExit(f"Requirement file not found: {req_path}")
        requirement = req_path.read_text(encoding="utf-8")
        req_name = req_path.stem

    run_id = args.run_id or slugify(req_name if req_name else "REQ")
    run_path = runs_dir() / run_id
    run_path.mkdir(parents=True, exist_ok=True)

    repo_map = get_repo_map()
    safe_print(f"Planning with {engine} [Requirement: {summarize_text(requirement)}]...")
    prompt = build_planner_prompt(requirement, engine, repo_map=repo_map)
    (run_path / "planner-prompt.md").write_text(prompt, encoding="utf-8")

    parsed, raw, error = run_planner(engine, prompt, cfg)
    (run_path / "planner-output.raw.txt").write_text(raw or error, encoding="utf-8")

    if parsed is None:
        if args.strict_planner or not cfg["planner"].get("allow_local_fallback", True):
            raise SystemExit(f"Planner failed: {error}\nRaw output saved to {run_path / 'planner-output.raw.txt'}")
        safe_print("Planner did not return valid task JSON. Using Sutra local fallback planner.")
        parsed = local_fallback_plan(requirement)

    plan = normalize_plan(parsed, run_id, "stdin" if args.input == "-" else str(req_path), engine, requirement)
    write_json(run_path / "task-plan.json", plan)
    write_json(run_path / "progress.json", {"run_id": run_id, "events": [], "updated_at": now_iso()})
    write_json(run_path / "token-ledger.json", {"run_id": run_id, "tasks": [], "updated_at": now_iso()})

    # Git branch creation.
    if is_git_repo() and not args.no_git_branch and cfg.get("git", {}).get("auto_branch"):
        prefix = cfg.get("git", {}).get("branch_prefix", "sutra/")
        branch_name = f"{prefix}{run_id}"
        safe_print(f"Creating git branch: {branch_name}")
        try:
            # Check if branch exists.
            cp = subprocess.run(["git", "rev-parse", "--verify", branch_name], capture_output=True, check=False)
            if cp.returncode == 0:
                subprocess.run(["git", "checkout", branch_name], check=True, capture_output=True)
            else:
                subprocess.run(["git", "checkout", "-b", branch_name], check=True, capture_output=True)
            plan["git_branch"] = branch_name
            write_json(run_path / "task-plan.json", plan)
        except Exception as exc:
            safe_print(f"Warning: Failed to create/checkout git branch {branch_name}: {exc}")

    show_tasks(plan, title=f"Generated Sutra task plan: {run_id}")
    
    # If we read from stdin, we likely can't do interactive confirmation if piped.
    is_interactive = sys.stdin.isatty()

    if not args.strict_planner and not confirm("Plan generated. Do you want to edit it?", assume_yes=not is_interactive):
        pass 
    else:
        if is_interactive and confirm("Interactive plan editing?", assume_yes=False):
            interactive_edit_plan(plan)
            write_json(run_path / "task-plan.json", plan)

    safe_print(f"\nPlan saved: {run_path / 'task-plan.json'}")
    if plan.get("planner_fallback"):
        safe_print("Note: local fallback planner was used. Run with --strict-planner to require Codex/Gemini JSON output.")
    
    safe_print("\n🚀 Next Steps (The Sutra Roadmap):")
    safe_print(f"  1. Validate: sutra validate --run {run_id}")
    safe_print(f"  2. Approve:  sutra approve --run {run_id}")
    safe_print(f"  3. Run:      sutra run --run {run_id} --step")
    safe_print(f"  4. Monitor:  sutra dashboard")


def load_plan(run_id: str) -> Tuple[Path, Dict[str, Any]]:
    ensure_initialized()
    run_path = runs_dir() / run_id
    plan_file = run_path / "task-plan.json"
    if not plan_file.exists():
        raise SystemExit(f"Task plan not found: {plan_file}")
    return run_path, read_json(plan_file, {})


def get_latest_run_id() -> Optional[str]:
    ensure_initialized()
    rdir = runs_dir()
    if not rdir.exists():
        return None
    runs = [d for d in rdir.iterdir() if d.is_dir() and (d / "task-plan.json").exists()]
    if not runs:
        return None
    # Sort by creation time of task-plan.json.
    runs.sort(key=lambda d: (d / "task-plan.json").stat().st_mtime, reverse=True)
    return runs[0].name


def resume_command(args: argparse.Namespace) -> None:
    run_id = args.run or get_latest_run_id()
    if not run_id:
        raise SystemExit("No runs found to resume. Provide --run or start a new one with --input.")
    
    safe_print(f"Resuming run: {run_id}")
    # Forward to run_command_main.
    args.run = run_id
    args.input = None
    run_command_main(args)


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
    safe_print(f"\nNext: sutra approve --run {args.run}")


def approve_command(args: argparse.Namespace) -> None:
    run_path, plan = load_plan(args.run)
    plan["approved"] = True
    plan["approved_at"] = now_iso()
    write_json(run_path / "task-plan.json", plan)
    safe_print(f"Approved run {args.run}.")
    safe_print(f"Next: sutra run --run {args.run}")


def build_claude_prompt(plan: Dict[str, Any], task: Dict[str, Any]) -> str:
    context_files_list = task.get("context_files", [])
    context_contents = []
    for f in context_files_list:
        fpath = cwd() / f
        if fpath.exists() and fpath.is_file():
            try:
                # Limit size to avoid blowing up prompt unnecessarily
                content = fpath.read_text(encoding="utf-8")
                if len(content) > 20000:
                    content = content[:20000] + "\n... [truncated]"
                context_contents.append(f"File: {f}\n```\n{content}\n```")
            except Exception:
                pass
    
    context_files_str = "\n\n".join(context_contents)
    criteria = "\n".join(f"- {c}" for c in task.get("success_criteria", []))
    validation = "\n".join(f"- {c}" for c in task.get("validation_commands", []))
    requirement = plan.get("requirement_excerpt", "")
    
    return f"""
# Sutra Operating Rules
- Execute only the assigned task.
- Do not expand scope.
- Do not run destructive commands.
- Keep changes small and task-scoped.
- **IMPORTANT**: Context files are provided below. Do not use 'Read' tool on these files unless you believe they have changed or you need to read a section beyond what was provided.
- Run the validation commands if applicable.
- Update docs/progress.md only if the task asks for progress/documentation update.

# Requirement Excerpt
{requirement}

# Context Files Content
{context_files_str}

# Sutra Claude Code Task Details
## Run
{plan.get('run_id')}: {plan.get('title')}

## Task
ID: {task.get('id')}
Title: {task.get('title')}

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


def create_checkpoint(run_id: str, task_id: str) -> None:
    if not is_git_repo():
        return
    msg = f"sutra(PRE): {run_id} - {task_id}"
    try:
        # Check if there are dirty changes to commit before task starts
        cp_status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, check=False)
        if cp_status.stdout.strip():
            subprocess.run(["git", "add", "."], check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", msg], check=True, capture_output=True)
            safe_print(f"  safety checkpoint created: {msg}")
    except Exception as exc:
        safe_print(f"  Warning: failed to create safety checkpoint: {exc}")


def run_task_internal(run_path: Path, plan: Dict[str, Any], task: Dict[str, Any], cfg: Dict[str, Any], dry_run: bool = False, no_git_commit: bool = False, hint: Optional[str] = None) -> str:
    if not dry_run and cfg.get("git", {}).get("auto_commit", True):
        create_checkpoint(plan.get("run_id", "REQ"), task.get("id", "T000"))
    
    prompt = build_claude_prompt(plan, task)
    if hint:
        prompt += f"\n\n## Developer Hint\n{hint}"
    task_dir = run_path / "tasks" / str(task.get("id"))
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "prompt.md").write_text(prompt, encoding="utf-8")

    safe_print(f"\n▶ Executing {task.get('id')}: {task.get('title')}")
    safe_print(f"  model={task.get('model')} timeout={task.get('timeout_seconds')}s max_turns={task.get('max_turns')}")
    safe_print(f"  prompt={summarize_text(prompt)} allowed_tools=" + ", ".join(task.get("allowed_tools", [])))

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

        # Git auto-commit.
        if status == "completed" and is_git_repo() and not no_git_commit and cfg.get("git", {}).get("auto_commit"):
            msg = f"sutra({plan.get('run_id')}): {task.get('id')} - {task.get('title')}"
            safe_print(f"  committing changes: {msg}")
            try:
                subprocess.run(["git", "add", "."], check=True, capture_output=True)
                # Check if there are changes to commit.
                cp_diff = subprocess.run(["git", "diff", "--cached", "--quiet"], check=False)
                if cp_diff.returncode != 0:
                    subprocess.run(["git", "commit", "-m", msg], check=True, capture_output=True)
                else:
                    safe_print("  no changes to commit.")
            except Exception as exc:
                safe_print(f"  Warning: git commit failed: {exc}")

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
    status = run_task_internal(run_path, plan, task, cfg, dry_run=args.dry_run, no_git_commit=getattr(args, 'no_git_commit', False))
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

    tasks = plan.get("tasks", [])
    i = 0
    while i < len(tasks):
        task = tasks[i]
        if task.get("status") == "completed" and not args.rerun_completed:
            safe_print(f"Skipping completed task {task.get('id')}")
            i += 1
            continue
        
        status = run_task_internal(run_path, plan, task, cfg, dry_run=args.dry_run, no_git_commit=args.no_git_commit)
        write_json(run_path / "task-plan.json", plan)

        if args.step:
            while True:
                safe_print(f"\nTask {task.get('id')} finished with status: {status} [Step Mode]")
                choice = input("[C]ontinue, [R]etry, [H]int & Retry, [D]iff, [E]dit plan, [A]bort: ").strip().lower()
                if choice == "c":
                    break
                elif choice == "r":
                    status = run_task_internal(run_path, plan, task, cfg, dry_run=args.dry_run, no_git_commit=args.no_git_commit)
                    write_json(run_path / "task-plan.json", plan)
                elif choice == "h":
                    hint = input("Enter hint for Claude: ").strip()
                    status = run_task_internal(run_path, plan, task, cfg, dry_run=args.dry_run, no_git_commit=args.no_git_commit, hint=hint)
                    write_json(run_path / "task-plan.json", plan)
                elif choice == "d":
                    subprocess.run(["git", "diff"])
                elif choice == "e":
                    interactive_edit_plan(plan)
                    write_json(run_path / "task-plan.json", plan)
                    tasks = plan.get("tasks", []) # Refresh tasks list
                elif choice == "a":
                    raise SystemExit("Aborted by user.")
                else:
                    safe_print("Unknown choice.")

        if status != "completed" and not args.dry_run:
            plan["status"] = "blocked"
            write_json(run_path / "task-plan.json", plan)
            safe_print(f"Run blocked at task {task.get('id')}. Review {run_path / 'tasks' / str(task.get('id'))}")
            raise SystemExit(1)
        
        i += 1

    plan["status"] = "completed" if not args.dry_run else "dry-run"
    plan["completed_at"] = now_iso()
    write_json(run_path / "task-plan.json", plan)
    safe_print(f"\nRun {run_id} finished with status: {plan['status']}")
    summarize_run(run_id)
    safe_print(f"Next: sutra dashboard OR sutra summarize --run {run_id}")


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


class SutraDashboardHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/runs":
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            rdir = runs_dir()
            runs = []
            if rdir.exists():
                for d in rdir.iterdir():
                    if d.is_dir() and (d / "task-plan.json").exists():
                        plan = read_json(d / "task-plan.json", {})
                        runs.append({
                            "id": d.name,
                            "title": plan.get("title"),
                            "status": plan.get("status"),
                            "created_at": plan.get("created_at")
                        })
            # Sort by created_at desc
            runs.sort(key=lambda x: x.get("created_at", ""), reverse=True)
            self.wfile.write(json.dumps(runs).encode())
            return

        if self.path.startswith("/api/run/"):
            run_id = self.path.replace("/api/run/", "")
            run_path = runs_dir() / run_id
            if not run_path.exists():
                self.send_error(404)
                return
            
            plan = read_json(run_path / "task-plan.json", {})
            ledger = read_json(run_path / "token-ledger.json", {"tasks": []})
            progress = read_json(run_path / "progress.json", {"events": []})
            
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "plan": plan,
                "ledger": ledger,
                "progress": progress
            }).encode())
            return

        # Main Dashboard UI
        if self.path == "/" or self.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            html = """
<!DOCTYPE html>
<html>
<head>
    <title>Sutra Dashboard</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; line-height: 1.6; color: #333; max-width: 1200px; margin: 0 auto; padding: 20px; background: #f4f7f6; }
        h1, h2 { color: #2c3e50; }
        .run-list { display: grid; grid-template-columns: 1fr 3fr; gap: 20px; }
        .sidebar { background: #fff; padding: 20px; border-radius: 8px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); height: fit-content; }
        .main-content { background: #fff; padding: 20px; border-radius: 8px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
        .run-item { padding: 10px; margin-bottom: 10px; border-bottom: 1px solid #eee; cursor: pointer; border-radius: 4px; transition: background 0.2s; }
        .run-item:hover { background: #f0f0f0; }
        .run-item.active { background: #3498db; color: #fff; }
        .status { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 0.8em; font-weight: bold; text-transform: uppercase; }
        .status-completed { background: #2ecc71; color: #fff; }
        .status-running { background: #3498db; color: #fff; }
        .status-blocked { background: #e74c3c; color: #fff; }
        .status-planned { background: #95a5a6; color: #fff; }
        .task-card { border: 1px solid #eee; padding: 15px; margin-bottom: 15px; border-radius: 6px; }
        .task-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
        .tokens-table { width: 100%; border-collapse: collapse; margin-top: 20px; }
        .tokens-table th, .tokens-table td { text-align: left; padding: 10px; border-bottom: 1px solid #eee; }
        .tokens-table th { background: #f9f9f9; }
        .savings { color: #27ae60; font-weight: bold; }
    </style>
</head>
<body>
    <h1>Sutra Dashboard</h1>
    <div class="run-list">
        <div class="sidebar" id="sidebar">
            <h3>Recent Runs</h3>
            <div id="runs-container">Loading runs...</div>
        </div>
        <div class="main-content" id="detail-container">
            <p>Select a run from the sidebar to view details.</p>
        </div>
    </div>

    <script>
        async function loadRuns() {
            const resp = await fetch('/api/runs');
            const runs = await resp.json();
            const container = document.getElementById('runs-container');
            container.innerHTML = '';
            runs.forEach(run => {
                const div = document.createElement('div');
                div.className = 'run-item';
                div.onclick = () => loadRunDetail(run.id, div);
                div.innerHTML = `
                    <strong>${run.id}</strong><br>
                    <small>${run.title || ''}</small><br>
                    <span class="status status-${run.status}">${run.status}</span>
                `;
                container.appendChild(div);
            });
        }

        async function loadRunDetail(runId, element) {
            document.querySelectorAll('.run-item').forEach(el => el.classList.remove('active'));
            element.classList.add('active');
            
            const resp = await fetch(`/api/run/${runId}`);
            const data = await resp.json();
            const container = document.getElementById('detail-container');
            
            let tasksHtml = data.plan.tasks.map(t => `
                <div class="task-card">
                    <div class="task-header">
                        <strong>${t.id}: ${t.title}</strong>
                        <span class="status status-${t.status}">${t.status}</span>
                    </div>
                    <div><small>Model: ${t.model} | Timeout: ${t.timeout_seconds}s | Max Turns: ${t.max_turns}</small></div>
                </div>
            `).join('');

            let tokensHtml = '';
            if (data.ledger && data.ledger.tasks && data.ledger.tasks.length > 0) {
                tokensHtml = `
                    <h2>Token Savings</h2>
                    <table class="tokens-table">
                        <thead>
                            <tr>
                                <th>Task</th>
                                <th>Model</th>
                                <th>Actual</th>
                                <th>Baseline</th>
                                <th>Saved</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${data.ledger.tasks.map(item => `
                                <tr>
                                    <td>${item.task_id}</td>
                                    <td>${item.model}</td>
                                    <td>${item.actual.input_tokens + item.actual.output_tokens}</td>
                                    <td>${item.baseline.estimated_tokens}</td>
                                    <td class="savings">${item.savings.tokens_saved} (${item.savings.percent_saved}%)</td>
                                </tr>
                            `).join('')}
                        </tbody>
                    </table>
                `;
            }

            container.innerHTML = `
                <h2>Run: ${runId}</h2>
                <p><strong>Status:</strong> <span class="status status-${data.plan.status}">${data.plan.status}</span></p>
                <p><strong>Planner:</strong> ${data.plan.engine}</p>
                
                <h3>Task Plan</h3>
                ${tasksHtml}
                
                ${tokensHtml}
            `;
        }

        loadRuns();
    </script>
</body>
</html>
            """
            self.wfile.write(html.encode())
            return
            
        return super().do_GET()


def dashboard_command(args: argparse.Namespace) -> None:
    ensure_initialized()
    port = args.port
    handler = SutraDashboardHandler
    
    # Run in a separate thread so it doesn't block (optional, but good for CLI UX)
    # However, for a simple CLI command, blocking is fine.
    
    safe_print(f"Starting Sutra Dashboard at http://localhost:{port}")
    safe_print("Press Ctrl+C to stop.")
    
    # Auto-open browser
    webbrowser.open(f"http://localhost:{port}")
    
    try:
        with socketserver.TCPServer(("", port), handler) as httpd:
            httpd.serve_forever()
    except KeyboardInterrupt:
        safe_print("\nDashboard stopped.")
        raise SystemExit(0)
    except Exception as exc:
        safe_print(f"Error starting dashboard: {exc}")
        raise SystemExit(1)


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


def start_command(args: argparse.Namespace) -> None:
    ensure_initialized()
    cfg = load_config()
    safe_print("🚀 Welcome to Sutra! Let's start your new task.")
    
    # 1. Ask for Task Name
    run_id = input("\n1. What is the name of this task? (e.g. ADD-AUTH): ").strip()
    if not run_id:
        run_id = f"RUN-{datetime.now().strftime('%m%d-%H%M')}"
        safe_print(f"   (No name provided, using generated ID: {run_id})")
    run_id = slugify(run_id)

    # 2. Ask for Requirement Source
    safe_print("\n2. How would you like to provide the requirement?")
    safe_print("   [1] Paste text (from clipboard/design doc)")
    safe_print("   [2] Provide path to a Markdown file")
    choice = input("Choice [1/2]: ").strip()

    input_source = "-"
    if choice == "2":
        input_source = input("   Path to file: ").strip()
        if not Path(input_source).exists():
            safe_print(f"   Error: File {input_source} not found. Falling back to paste mode.")
            input_source = "-"

    # 3. Ask for Engine (or use default)
    engine = cfg.get("default_engine", "codex")
    safe_print(f"\n3. Which planning engine should I use? (default: {engine})")
    custom_engine = input(f"   Press Enter for {engine}, or type [gemini/codex]: ").strip().lower()
    if custom_engine in {"gemini", "codex"}:
        engine = custom_engine

    # 4. Confirm and Run Plan
    safe_print(f"\nReady! Running: sutra plan --input {input_source} --engine {engine} --run-id {run_id}")
    
    # Construct args for plan_command
    plan_args = argparse.Namespace(
        input=input_source,
        engine=engine,
        run_id=run_id,
        strict_planner=False,
        no_git_branch=False
    )
    
    plan_command(plan_args)


def update_command(args: argparse.Namespace) -> None:
    safe_print("Checking for updates...")
    
    # 1. Check if we are running from a git clone (editable install).
    if (Path(__file__).parent.parent / ".git").exists():
        safe_print("Detected editable/git installation. Pulling latest changes...")
        try:
            subprocess.run(["git", "pull"], check=True, cwd=str(Path(__file__).parent.parent))
            subprocess.run([sys.executable, "-m", "pip", "install", "-e", "."], check=True, cwd=str(Path(__file__).parent.parent))
            safe_print("Successfully updated from git.")
        except Exception as exc:
            safe_print(f"Update failed: {exc}")
        return

    # 2. Check for pipx.
    is_pipx = "pipx" in sys.executable or "/.local/share/pipx/" in sys.executable
    if is_pipx:
        safe_print("Detected pipx installation. Updating via pipx...")
        try:
            subprocess.run(["pipx", "upgrade", "sutra-cli"], check=True)
            safe_print("Successfully updated via pipx.")
        except Exception as exc:
            safe_print(f"Pipx update failed: {exc}")
        return

    # 3. Default to pip update.
    safe_print("Updating via pip...")
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "sutra-cli"]
    
    # Check for PEP 668 externally managed environment.
    if sys.platform == "darwin" or os.path.exists("/etc/os-release"):
        cmd.append("--break-system-packages")
        
    try:
        subprocess.run(cmd, check=True)
        safe_print("Successfully updated via pip.")
    except Exception as exc:
        safe_print(f"Pip update failed: {exc}")
        safe_print("\nTip: If you are on macOS, we recommend using 'pipx upgrade sutra-cli' or 'brew install pipx' if you haven't yet.")


class SutraArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # Find close matches for subcommands or options
        suggestion = ""
        words = message.split()
        
        # Check for invalid choice errors
        if "invalid choice: " in message:
            invalid_choice = re.search(r"invalid choice: '([^']+)'", message)
            if invalid_choice:
                choice = invalid_choice.group(1)
                # Look for subcommands in the subparsers
                for action in self._actions:
                    if isinstance(action, argparse._SubParsersAction):
                        matches = difflib.get_close_matches(choice, action.choices.keys(), n=1, cutoff=0.6)
                        if matches:
                            suggestion = f"\n\nDid you mean: {matches[0]}?"
                            break
        
        # Check for unrecognized arguments
        elif "unrecognized arguments: " in message:
            unrecognized_part = message.split("unrecognized arguments: ")[1]
            unrecognized = unrecognized_part.split()
            
            potential_flags = []
            for action in self._actions:
                potential_flags.extend(action.option_strings)
            
            # If we are in a sub-parser, also look at parent parser flags if any
            # But more importantly, if we missed flags, it might be because they are in sub-parsers
            # that we haven't traversed. For now, let's just make sure we capture all from current.
            
            suggestions = []
            for arg in unrecognized:
                if arg.startswith("-"):
                    matches = difflib.get_close_matches(arg, potential_flags, n=1, cutoff=0.6)
                    if matches:
                        suggestions.append(f"{arg} -> {matches[0]}")
            
            if suggestions:
                suggestion = f"\n\nDid you mean:\n  " + "\n  ".join(suggestions)

        sys.stderr.write(f"{self.prog}: error: {message}{suggestion}\n")
        self.exit(2)


def build_parser() -> argparse.ArgumentParser:
    parser = SutraArgumentParser(prog="sutra", description="Sutra: governed AI coding flow for Codex/Gemini and Claude Code")
    sub = parser.add_subparsers(dest="command", required=True)
    sub._parser_class = SutraArgumentParser # Ensure sub-parsers use our custom class

    p = sub.add_parser("init", help="Initialize Sutra files in the current repo")
    p.add_argument("--force", action="store_true", help="Refresh templates/config")
    p.set_defaults(func=init_project)

    p = sub.add_parser("start", help="Interactive wizard to start a new task")
    p.set_defaults(func=start_command)

    p = sub.add_parser("update", help="Update Sutra CLI to the latest version")
    p.set_defaults(func=update_command)

    p = sub.add_parser("doctor", help="Validate Codex/Gemini and Claude Code chain")
    p.add_argument("--engine", choices=["codex", "gemini"], default=None)
    p.add_argument("--smoke-test", action="store_true", help="Run headless smoke tests for planner and Claude")
    p.set_defaults(func=doctor)

    p = sub.add_parser("plan", help="Generate bounded Claude Code task plan")
    p.add_argument("--input", required=True, help="Requirement markdown file")
    p.add_argument("--engine", choices=["codex", "gemini"], default=None)
    p.add_argument("--run-id", default=None)
    p.add_argument("--strict-planner", action="store_true", help="Fail if planner does not return valid JSON")
    p.add_argument("--no-git-branch", action="store_true", help="Skip creating a git branch")
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
    p.add_argument("--no-git-commit", action="store_true", help="Skip committing after each task")
    p.add_argument("--step", action="store_true", help="Pause after each task for review")
    p.set_defaults(func=run_command_main)

    p = sub.add_parser("resume", help="Resume the latest or a specific run")
    p.add_argument("--run", help="Run ID to resume (defaults to latest)")
    p.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")
    p.add_argument("--auto-approve", action="store_true")
    p.add_argument("--skip-doctor", action="store_true")
    p.add_argument("--smoke-test", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--rerun-completed", action="store_true")
    p.add_argument("--no-git-commit", action="store_true")
    p.add_argument("--step", action="store_true", help="Pause after each task for review")
    p.set_defaults(func=resume_command)

    p = sub.add_parser("status", help="Show run status and tasks")
    p.add_argument("--run", required=True)
    p.set_defaults(func=status_command)

    p = sub.add_parser("summarize", help="Generate run summary")
    p.add_argument("--run", required=True)
    p.set_defaults(func=summarize_command)

    p = sub.add_parser("dashboard", help="Launch local web dashboard to view runs")
    p.add_argument("--port", type=int, default=8080, help="Port to run the dashboard on")
    p.set_defaults(func=dashboard_command)

    p = sub.add_parser("tokens", help="Token ledger commands")
    token_sub = p.add_subparsers(dest="tokens_command", required=True)
    token_sub._parser_class = SutraArgumentParser # Ensure token sub-parsers also use it
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
