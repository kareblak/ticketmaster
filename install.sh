#!/usr/bin/env bash
# Install (or reinstall) ticketmaster from this clone as an editable pipx app, then the skill.
# Without a clone: pipx install git+https://github.com/kareblak/ticketmaster.git && ticketmaster install-skill
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
command -v pipx >/dev/null || { echo "pipx is required: brew install pipx (or python3 -m pip install --user pipx)" >&2; exit 1; }

# Older installs symlinked ~/.local/bin/ticketmaster to the repo; pipx won't overwrite that.
old="$HOME/.local/bin/ticketmaster"
if [ -L "$old" ] && [[ "$(readlink "$old")" == *"/skills/ticketmaster/scripts/ticketmaster" ]]; then
  rm "$old"
fi

pipx install --force --editable "$root"
ticketmaster install-skill
