"""Run one configured harness CLI inside a disposable worker container."""

import json
import os
import subprocess
import sys


def run_harness(task: str) -> str:
    try:
        command = json.loads(os.environ["HARNESS_COMMAND_JSON"])
    except (KeyError, json.JSONDecodeError) as exc:
        raise ValueError("HARNESS_COMMAND_JSON must be a JSON array") from exc
    if not isinstance(command, list) or not command or not all(
        isinstance(arg, str) and arg for arg in command
    ):
        raise ValueError("HARNESS_COMMAND_JSON must contain command arguments")

    has_task_argument = "{task}" in command
    if "{model}" in command and not os.getenv("HARNESS_MODEL"):
        raise ValueError("HARNESS_MODEL is required by the command")
    command = [
        task if arg == "{task}" else os.environ["HARNESS_MODEL"] if arg == "{model}" else arg
        for arg in command
    ]
    aliases = json.loads(os.getenv("HARNESS_ENV_ALIASES_JSON", "{}"))
    if not isinstance(aliases, dict) or not all(
        isinstance(target, str) and isinstance(source, str)
        for target, source in aliases.items()
    ):
        raise ValueError("HARNESS_ENV_ALIASES_JSON must be an object of variable names")
    child_env = os.environ.copy()
    for target, source in aliases.items():
        if not child_env.get(source):
            raise ValueError(f"required environment variable is missing: {source}")
        child_env[target] = child_env[source]
    completed = subprocess.run(
        command,
        input="" if has_task_argument else task,
        text=True,
        capture_output=True,
        check=False,
        cwd="/workspace",
        env=child_env,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"harness exited with status {completed.returncode}")
    answer = completed.stdout.strip()
    if not answer:
        raise RuntimeError("harness returned an empty answer")
    return answer


def main() -> None:
    try:
        request = json.load(sys.stdin)
        task = request["task"]
        if not isinstance(task, str) or not task.strip():
            raise ValueError("task is required")
        role_config = json.loads(os.getenv("HARNESS_ROLE_JSON", "{}"))
        if os.getenv("HARNESS_SESSION_MODE") == "http":
            from http_harness import run_http
            answer = run_http(task, role_config)
        else:
            instructions = role_config.get("instructions", "")
            answer = run_harness(f"Instructions: {instructions}\n\nTask: {task}" if instructions else task)
        print(json.dumps({"answer": answer}, ensure_ascii=False), flush=True)
    except (KeyError, TypeError, ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        print(f"Harness failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
