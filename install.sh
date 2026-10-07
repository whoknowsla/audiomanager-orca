#!/usr/bin/env bash
# Installs the Audio Manager extension into Orca's user extensions folder and
# approves it. Orca loads it the next time it starts; to restart Orca now,
# run: orca --replace &
#
# Run this again after changing the extension: any change alters the
# extension's hash, and Orca will not load it until it is approved again.

set -euo pipefail

source_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/audio_manager"
target_root="${XDG_DATA_HOME:-$HOME/.local/share}/orca/extensions"
target_dir="$target_root/audio_manager"

if [ ! -f "$source_dir/__init__.py" ]; then
    echo "Cannot find audio_manager/__init__.py next to this script." >&2
    exit 1
fi

for required in pw-dump wpctl pw-metadata; do
    if ! command -v "$required" >/dev/null 2>&1; then
        echo "Warning: $required is not on PATH. The extension needs it at runtime." >&2
    fi
done

if [ -e "$target_dir" ]; then
    printf 'Replace the existing extension at %s? [y/N] ' "$target_dir"
    read -r reply
    case "$reply" in
        [yY]*) ;;
        *) echo "Nothing was changed."; exit 0 ;;
    esac
    rm -rf -- "$target_dir"
fi

mkdir -p "$target_root"
cp -r -- "$source_dir" "$target_dir"
find "$target_dir" -name '__pycache__' -type d -prune -exec rm -rf -- {} +

orca --approve-extension audio_manager
echo "Installed Audio Manager in $target_dir. Restart Orca to load it."
