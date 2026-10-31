#!/bin/sh
# Install a verified ai-comm pin into a new project. See scripts/install_project.py.
set -eu
here=$(cd -- "$(dirname -- "$0")" && pwd -P)
exec python3 -I "$here/scripts/install_project.py" "$@"
