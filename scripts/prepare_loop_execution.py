"""Prepare pinned native context, raw broker audit, fills and runner index."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from operations.loop_integration.execution_summary import (
    export_native_context,
    load_execution_index,
    read_pinned,
    write_pinned_json,
)
from scripts.export_loop_broker_fills import run as export_fills


def run(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--config-sha256", required=True)
    parser.add_argument("--trade-date", type=date.fromisoformat, required=True)
    args = parser.parse_args(argv)
    config = json.loads(read_pinned(args.config, args.config_sha256))

    def local_path(value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else args.config.parent / path

    context_args = dict(config["context"])
    for name in ("plan_path", "confirmation_path", "first_pool_path"):
        context_args[name] = local_path(context_args[name])
    for name in ("selection_cutoff_utc", "as_of"):
        context_args[name] = datetime.fromisoformat(context_args[name])
    context = export_native_context(**context_args)
    if context.trade_date != args.trade_date:
        raise ValueError("provider config trade date mismatch")
    directory = local_path(config["output_dir"])
    context_path, context_hash = write_pinned_json(
        directory, "review-context", context.model_dump(mode="json"),
    )
    command = [
        "--plan", str(context_args["plan_path"]),
        "--plan-sha256", context_args["plan_sha256"],
        "--review-context", str(context_path), "--review-context-sha256", context_hash,
        "--trade-date", str(args.trade_date), "--as-of", context_args["as_of"].isoformat(),
        "--ledger", str(local_path(config["ledger_path"])),
        "--ledger-sha256", config["ledger_sha256"], "--output-dir", str(directory),
    ]
    if config.get("read_paper_broker") is True:
        if config.get("broker_export_path"):
            raise ValueError("provider must choose Paper reads or offline broker export")
        command += ["--read-paper-broker", "--metadata", str(local_path(config["metadata_path"])),
                    "--metadata-sha256", config["metadata_sha256"]]
    else:
        command += ["--broker-export", str(local_path(config["broker_export_path"])),
                    "--broker-export-sha256", config["broker_export_sha256"]]
    receipt = export_fills(command)
    previous_path = config.get("prior_execution_index_path")
    previous = load_execution_index(
        local_path(previous_path) if previous_path else None,
        config.get("prior_execution_index_sha256"),
    )
    entries = [entry.model_dump(mode="json") for entry in previous
               if (entry.trade_date, entry.strategy_sha256)
               != (context.trade_date, context.strategy_sha256)]
    entries.append({
        "trade_date": str(context.trade_date), "strategy_sha256": context.strategy_sha256,
        "plan_path": str(context_args["plan_path"].resolve()),
        "plan_sha256": context_args["plan_sha256"],
        "review_context_path": str(context_path), "review_context_sha256": context_hash,
        "fills_path": receipt["fills_path"], "fills_sha256": receipt["fills_sha256"],
    })
    index_path, index_hash = write_pinned_json(
        directory, "execution-index", {"executions": entries},
    )
    return {**receipt, "execution_index_path": str(index_path),
            "execution_index_sha256": index_hash, "provider_config_sha256": args.config_sha256}


def main() -> None:
    print(json.dumps(run()))


if __name__ == "__main__":
    main()
