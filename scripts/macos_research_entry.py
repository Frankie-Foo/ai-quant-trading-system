"""PyInstaller entry that also preserves ``python -m`` child dispatch."""

from __future__ import annotations

import runpy
import sys


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "-m":
        module = sys.argv[2]
        if not module.startswith(("data_plane.", "scripts.")):
            raise ValueError("frozen child module is not allowed")
        sys.argv = [module, *sys.argv[3:]]
        runpy.run_module(module, run_name="__main__", alter_sys=True)
        return 0

    from scripts.serve_macos_research_runtime import main as serve

    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
