#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
client_root="$(pwd)"
repo_root="$(cd .. && pwd)"
output="$client_root/build/bootstrap/research-bootstrap.zip"
source_archive="${BOOTSTRAP_ARCHIVE:-}"
source_data_root="${BOOTSTRAP_DATA_ROOT:-$repo_root/data}"

mkdir -p "$(dirname "$output")"

if command -v python >/dev/null 2>&1; then
  python_bin="python"
elif command -v python3 >/dev/null 2>&1; then
  python_bin="python3"
else
  echo "Python 3 is required to prepare the macOS bootstrap archive." >&2
  exit 2
fi
if [[ -n "$source_archive" ]]; then
  if [[ ! -f "$source_archive" ]]; then
    echo "BOOTSTRAP_ARCHIVE does not exist: $source_archive" >&2
    exit 2
  fi
  source_archive="$(cd "$(dirname "$source_archive")" && pwd)/$(basename "$source_archive")"
  if [[ "$source_archive" != "$output" ]]; then
    cp "$source_archive" "$output"
  fi
elif [[ -d "$source_data_root/accepted" ]]; then
  (
    cd "$repo_root"
    "$python_bin" -m scripts.build_desktop_bootstrap \
      --source-data-root "$source_data_root" \
      --output "$output"
  )
elif [[ -f "$output" ]]; then
  echo "Reusing existing bootstrap archive: $output"
else
  cat >&2 <<EOF
macOS bootstrap data is missing.
Provide one of:
  BOOTSTRAP_ARCHIVE=/absolute/path/research-bootstrap.zip
  BOOTSTRAP_DATA_ROOT=/absolute/path/to/data
The build is stopped so it cannot produce an app without stock history.
EOF
  exit 2
fi

"$python_bin" - "$output" <<'PY'
from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path

archive_path = Path(sys.argv[1]).resolve()
with zipfile.ZipFile(archive_path) as archive:
    bad_member = archive.testzip()
    if bad_member is not None:
        raise SystemExit(f"bootstrap ZIP CRC failed: {bad_member}")
    try:
        manifest = json.loads(archive.read("bootstrap-manifest.json"))
    except (KeyError, ValueError, UnicodeDecodeError) as exc:
        raise SystemExit("bootstrap manifest is invalid") from exc
    files = manifest.get("files")
    if (
        manifest.get("schema_version") != "desktop_bootstrap.v1"
        or not isinstance(files, list)
        or not files
        or int(manifest.get("dataset_count", 0)) <= 0
    ):
        raise SystemExit("bootstrap manifest is empty or incompatible")
    names = set(archive.namelist())
    for value in files:
        relative = str(value.get("path", ""))
        if relative not in names:
            raise SystemExit(f"bootstrap member is missing: {relative}")
        payload = archive.read(relative)
        if len(payload) != int(value.get("size", -1)):
            raise SystemExit(f"bootstrap member size mismatch: {relative}")
        if hashlib.sha256(payload).hexdigest() != str(value.get("sha256", "")):
            raise SystemExit(f"bootstrap member hash mismatch: {relative}")
print(
    json.dumps(
        {
            "ok": True,
            "archive": str(archive_path),
            "dataset_count": int(manifest["dataset_count"]),
            "file_count": len(files),
            "archive_bytes": archive_path.stat().st_size,
        },
        ensure_ascii=False,
    )
)
PY
