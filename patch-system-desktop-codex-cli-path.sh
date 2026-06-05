#!/bin/sh
set -eu

desktop_file="${1:-/usr/share/applications/codex-desktop.desktop}"
codex_cli_path="${CODEX_CLI_PATH_OVERRIDE:-/home/arnab/.vite-plus/bin/codex}"

if [ ! -f "$desktop_file" ]; then
    printf 'Desktop file does not exist yet: %s\n' "$desktop_file" >&2
    exit 1
fi

desktop_dir="$(dirname "$desktop_file")"
patched_file="$(mktemp)"
trap 'rm -f "$patched_file"' EXIT INT HUP TERM

awk -v cli_path="$codex_cli_path" '
    /^Exec=.*\/usr\/bin\/codex-desktop([[:space:]]|$)/ {
        gsub(/[[:space:]]+CODEX_CLI_PATH="[^"]*"/, "")
        gsub(/[[:space:]]+CODEX_CLI_PATH=[^[:space:]]+/, "")

        if ($0 ~ /^Exec=env[[:space:]]/) {
            sub(/[[:space:]]+\/usr\/bin\/codex-desktop/, " CODEX_CLI_PATH=\"" cli_path "\" /usr/bin/codex-desktop")
        } else {
            sub(/^Exec=/, "Exec=env CODEX_CLI_PATH=\"" cli_path "\" ")
        }
    }
    { print }
' "$desktop_file" > "$patched_file"

if cmp -s "$desktop_file" "$patched_file"; then
    printf 'No changes needed: %s already uses CODEX_CLI_PATH="%s"\n' "$desktop_file" "$codex_cli_path"
    exit 0
fi

sudo install -m 0644 "$patched_file" "$desktop_file"

if command -v update-desktop-database >/dev/null 2>&1; then
    sudo update-desktop-database "$desktop_dir" >/dev/null 2>&1 || true
fi

printf 'Patched %s with CODEX_CLI_PATH="%s"\n' "$desktop_file" "$codex_cli_path"
