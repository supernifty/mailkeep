#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${MAILKEEP_PYTHON:-$repo_dir/.venv/bin/python}"
config="${MAILKEEP_CONFIG:-$repo_dir/config.yaml}"

[[ -x "$python_bin" ]] || { echo "Mailkeep Python not found: $python_bin" >&2; exit 2; }

MAILKEEP_CONFIG="$config" exec "$python_bin" -m mailkeep.schedule_status
