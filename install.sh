#!/usr/bin/env bash
# Creates .venv with the board's dependencies and links the skill into ~/.claude/skills.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python="${PYTHON:-python3}"

"$python" -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
  || { echo "ticketmaster needs Python 3.11+ (found $("$python" --version))" >&2; exit 1; }

[ -d "$root/.venv" ] || "$python" -m venv "$root/.venv"
"$root/.venv/bin/pip" install --quiet --upgrade pip
"$root/.venv/bin/pip" install --quiet -r "$root/requirements.txt"

skills_dir="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
mkdir -p "$skills_dir"
target="$skills_dir/ticketmaster"
if [ -e "$target" ] && [ ! -L "$target" ]; then
  echo "$target exists and is not a symlink; remove it and re-run" >&2
  exit 1
fi
ln -sfn "$root/skills/ticketmaster" "$target"

if [ -d "$HOME/.local/bin" ]; then
  ln -sfn "$root/skills/ticketmaster/scripts/ticketmaster" "$HOME/.local/bin/ticketmaster"
  echo "Linked ~/.local/bin/ticketmaster"
fi
echo "Installed. Use /ticketmaster in Claude Code, or run: ticketmaster [parent] [--label x]"
