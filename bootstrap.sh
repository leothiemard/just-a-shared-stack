#!/bin/sh
# Set up a box: clone just-a-stack (unless already there), then run `jas init` with the given arguments.
# Usage: curl -fsSL https://raw.githubusercontent.com/<owner>/just-a-stack/main/bootstrap.sh | sh -s -- --box linux-box --zone personal --brain <git-url> --apply
set -eu

repo=${JAS_STACK_URL:-https://github.com/leothiemard/just-a-stack.git}
dir=${JAS_STACK_DIR:-$HOME/Development/just-a-stack}

command -v git >/dev/null 2>&1 || { echo "bootstrap: git is required. Install it, then rerun." >&2; exit 1; }

python=""
for candidate in python3 /usr/bin/python3; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
    python=$candidate
    break
  fi
done
[ -n "$python" ] || { echo "bootstrap: Python 3.9 or newer is required. Install it, then rerun." >&2; exit 1; }

if [ -d "$dir/.git" ]; then
  echo "Using the existing checkout at $dir"
else
  git clone -q "$repo" "$dir"
fi

JAS_PYTHON=$python exec "$dir/bin/jas" init "$@"
