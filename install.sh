#!/usr/bin/env bash
# Install (or reinstall) ticketmaster from this clone as an editable pipx app, then link the skill to the clone.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
command -v pipx >/dev/null || { echo "pipx is required: brew install pipx (or python3 -m pip install --user pipx)" >&2; exit 1; }

# Older installs symlinked ~/.local/bin/ticketmaster to the repo; pipx won't overwrite that.
old="$HOME/.local/bin/ticketmaster"
if [ -L "$old" ] && [[ "$(readlink "$old")" == *"/skills/ticketmaster/scripts/ticketmaster" ]]; then
  rm "$old"
fi

pipx install --force --editable "$root"

skills="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
mkdir -p "$skills"
# A copy left by the old `ticketmaster install-skill` is replaced by a link to this clone.
if [ -d "$skills/ticketmaster" ] && [ ! -L "$skills/ticketmaster" ]; then
  rm -r "$skills/ticketmaster"
fi
ln -sfn "$root/ticketmaster/skill" "$skills/ticketmaster"
echo "Linked the /ticketmaster skill at $skills/ticketmaster. Restart Claude Code sessions to pick it up."
