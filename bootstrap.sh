#!/bin/sh
# Set up a box: clone just-a-stack (unless already there), then run `jass init` with the given arguments.
# Usage: curl -fsSL https://raw.githubusercontent.com/<owner>/just-a-stack/main/bootstrap.sh | sh -s -- --box linux-box --zone personal --brain <git-url> --apply
set -eu

repo=${JASS_STACK_URL:-https://github.com/leothiemard/just-a-stack.git}
dir=${JASS_STACK_DIR:-$HOME/Development/just-a-stack}

command -v git >/dev/null 2>&1 || { echo "bootstrap: git is required. Install it, then rerun." >&2; exit 1; }

python=""
for candidate in python3 /usr/bin/python3; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
    python=$candidate
    break
  fi
done
[ -n "$python" ] || { echo "bootstrap: Python 3.9 or newer is required. Install it, then rerun." >&2; exit 1; }

# A repository URL as host/owner/repo in lower case, whatever its form (https, ssh://, scp-like git@host:owner/repo).
repo_id() {
  printf '%s\n' "$1" | sed -e 's#^[A-Za-z][A-Za-z0-9+.-]*://##' -e 's#^[^@/]*@##' -e 's#:[0-9]*/#/#' -e 's#:#/#' -e 's#/*$##' -e 's#\.git$##' | tr 'A-Z' 'a-z'
}

if [ -d "$dir" ] && [ -n "$(ls -A "$dir")" ]; then
  top=$(git -C "$dir" rev-parse --show-toplevel 2>/dev/null) || top=""
  origin=$(git -C "$dir" remote get-url origin 2>/dev/null) || origin=""
  if [ "$top" != "$(cd "$dir" && pwd -P)" ] || [ "$(repo_id "$origin")" != "$(repo_id "$repo")" ] || [ ! -x "$dir/bin/jass" ] || [ ! -f "$dir/jass/cli.py" ]; then
    echo "bootstrap: $dir is not a checkout of $repo. Move it away, or set JASS_STACK_DIR to another folder, then rerun." >&2
    exit 1
  fi
  echo "Using the existing checkout at $dir"
else
  git clone -q "$repo" "$dir"
fi

JASS_PYTHON=$python exec "$dir/bin/jass" init "$@"
