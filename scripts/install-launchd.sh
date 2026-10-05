#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
label="local.mailkeep.daily"
template="$repo_dir/ops/launchd/$label.plist.in"
target="$HOME/Library/LaunchAgents/$label.plist"
log_dir="${MAILKEEP_LOG_DIR:-$repo_dir/mailkeep-data/logs}"
staging_dir="$repo_dir/tmp/codex"
staging="$staging_dir/$label.plist"
domain="gui/$(id -u)"

mkdir -p "$log_dir" "$staging_dir" "$(dirname -- "$target")"
chmod 700 "$log_dir"
cp "$template" "$staging"
trap 'rm -f "$staging"' EXIT

/usr/bin/plutil -remove ProgramArguments.0 "$staging"
/usr/bin/plutil -insert ProgramArguments.0 -string \
    "$repo_dir/scripts/mailkeep-periodic.sh" "$staging"
/usr/bin/plutil -replace WorkingDirectory -string "$repo_dir" "$staging"
/usr/bin/plutil -replace StandardOutPath -string \
    "$log_dir/periodic.out.log" "$staging"
/usr/bin/plutil -replace StandardErrorPath -string \
    "$log_dir/periodic.err.log" "$staging"
/usr/bin/plutil -lint "$staging"

/usr/bin/install -m 600 "$staging" "$target"
/bin/launchctl bootout "$domain/$label" 2>/dev/null || true
/bin/launchctl bootstrap "$domain" "$target"
/bin/launchctl print "$domain/$label"
