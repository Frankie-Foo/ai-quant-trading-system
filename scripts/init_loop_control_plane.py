from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from operations.local_env import load_project_env
from operations.loop_integration.client import LoopClient
from operations.loop_integration.control_plane import LoopControlPlaneManifest

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Explicitly initialize immutable Loop quant control contracts."
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ROOT / "config/loop_control_plane/us_equity.v1.json",
    )
    parser.add_argument(
        "--binding-out",
        type=Path,
        default=ROOT / "runs/loop-control-plane/us_equity.binding.json",
    )
    args = parser.parse_args()
    load_project_env(ROOT)
    manifest = LoopControlPlaneManifest.model_validate_json(
        args.manifest.read_text(encoding="utf-8")
    )
    client = LoopClient(
        base_url=os.environ.get("LOOP_BASE_URL", ""),
        api_key=os.environ.get("LOOP_RUNTIME_API_KEY", ""),
    )
    binding = client.initialize_control_plane(manifest)
    args.binding_out.parent.mkdir(parents=True, exist_ok=True)
    args.binding_out.write_text(binding.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": "initialized",
                "manifest_version": manifest.version,
                "market_scope": manifest.market_scope,
                "binding_path": str(args.binding_out),
            }
        )
    )


if __name__ == "__main__":
    main()
