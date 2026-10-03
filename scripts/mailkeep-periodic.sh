#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
mailkeep_bin="${MAILKEEP_BIN:-$repo_dir/.venv/bin/mailkeep}"
config="${MAILKEEP_CONFIG:-$repo_dir/config.yaml}"

[[ -x "$mailkeep_bin" ]] || { echo "mailkeep executable not found: $mailkeep_bin" >&2; exit 2; }
[[ -f "$config" ]] || { echo "mailkeep config not found: $config" >&2; exit 2; }

"$mailkeep_bin" --config "$config" archive
"$mailkeep_bin" --config "$config" prune --execute
