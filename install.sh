#!/bin/sh
# Install a verified ai-comm release.
#   install.sh --project DIR --version X --fetch-permitted   into a project's pin (scripts/install_project.py)
#   install.sh --user <command> ...                           the per-user cache (scripts/install_user.py)
#   install.sh --user wake plan|install|remove|integration    optional wake adapters (scripts/wake.py; docs/WAKE.md)
set -eu
here=$(cd -- "$(dirname -- "$0")" && pwd -P)
if [ "${1:-}" = "--user" ]; then
  shift
  exec python3 -I "$here/scripts/install_user.py" "$@"
fi
exec python3 -I "$here/scripts/install_project.py" "$@"
