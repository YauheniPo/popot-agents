"""Small offline CLI used to verify that the worker really invokes a harness."""

import sys


def main() -> None:
    task = sys.stdin.read().strip()
    if "\n\nUser: " in task:
        task = task.rsplit("\n\nUser: ", 1)[1].split("\n\nAssistant:", 1)[0].strip()
    if not task.lower().startswith("sum:"):
        print("supported task: sum: 2, 3, 5", file=sys.stderr)
        raise SystemExit(2)
    try:
        numbers = [int(part.strip()) for part in task[4:].split(",")]
    except ValueError:
        print("sum: requires comma-separated integers", file=sys.stderr)
        raise SystemExit(2)
    print(sum(numbers))


if __name__ == "__main__":
    main()
