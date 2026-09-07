"""Read explicit pinned native inputs and print context JSON; no env or network."""

import argparse
from datetime import datetime
from pathlib import Path

from operations.loop_integration.execution_summary import export_native_context


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("plan", "confirmation", "first-pool"):
        parser.add_argument(f"--{name}", required=True, type=Path, dest=name.replace("-", "_")
                            + "_path")
        parser.add_argument(f"--{name}-sha256", required=True)
    parser.add_argument("--active-policy-hash", required=True)
    parser.add_argument("--selection-cutoff-utc", required=True, type=datetime.fromisoformat)
    parser.add_argument("--as-of", required=True, type=datetime.fromisoformat)
    print(export_native_context(**vars(parser.parse_args())).model_dump_json())


if __name__ == "__main__":
    main()
