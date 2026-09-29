"""Windowless Windows Task Scheduler entry point for the four owned lanes."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

RUNNERS = {
    "run_modern_funnel_tick.ps1",
    "run_postmarket_tick.ps1",
    "run_monthly_evolution_tick.ps1",
    "run_research_cycle_tick.ps1",
}


def run(arguments: list[str]) -> int:
    scripts_dir = Path(__file__).resolve().parent
    log_dir = scripts_dir.parent / "runs"
    try:
        if not arguments:
            raise ValueError("missing task runner")
        runner = Path(arguments[0]).resolve(strict=True)
        if runner.parent != scripts_dir or runner.name not in RUNNERS:
            raise ValueError("task runner is not owned by this release")
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / f"{runner.stem}.launcher.log").open("ab") as output:
            completed = subprocess.run(
                [
                    "powershell.exe", "-NoProfile", "-NonInteractive",
                    "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass",
                    "-File", str(runner), *arguments[1:],
                ],
                cwd=scripts_dir.parent,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
            if completed.returncode:
                output.write(f"launcher exit_code={completed.returncode}\n".encode("ascii"))
            return completed.returncode
    except Exception as exc:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            with (log_dir / "hidden_task_launcher.err.log").open("a", encoding="utf-8") as output:
                output.write(f"{type(exc).__name__}: {exc}\n")
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
