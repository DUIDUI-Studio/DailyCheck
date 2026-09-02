#!/bin/bash
# DailyCheck — install git hooks from scripts/git-hooks/.
#
# Two modes:
#   (no args)        → LOCAL install: copies hooks to THIS repo's
#                      .git/hooks/ (overrides git's default location).
#                      Won't affect any other repo.
#   --global         → GLOBAL install: copies hooks to the shared
#                      user-level directory and sets
#                      `core.hooksPath` globally so every repo picks
#                      up the same hooks.
#
# Pick --global if you want the rule to persist across all your
# projects (recommended for Eric — see ~/.workbuddy/MEMORY.md
# "Git push 必须先确认"). Pick no-args if you only want this repo.
#
# Both modes are idempotent. Re-running copies the latest hook
# script into place. The script itself lives at
# scripts/git-hooks/pre-push in this repo (or the canonical copy
# in ~/.workbuddy/git-hooks/).
#
# Bypass (only after Eric explicitly says yes):
#     git push --no-verify

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
HOOKS_SRC="$SCRIPT_DIR/git-hooks"

GLOBAL_DIR="${HOME}/.workbuddy/git-hooks"

if [ ! -d "$HOOKS_SRC" ]; then
  echo "!! 找不到 $HOOKS_SRC" >&2
  exit 1
fi

install_local() {
  local hooks_dst="$REPO_DIR/.git/hooks"
  if [ ! -d "$hooks_dst" ]; then
    echo "!! $hooks_dst 不存在, 不是 git 仓库?" >&2
    exit 1
  fi
  local n=0
  for src in "$HOOKS_SRC"/*; do
    cp "$src" "$hooks_dst/$(basename "$src")"
    chmod +x "$hooks_dst/$(basename "$src")"
    n=$((n + 1))
  done
  echo "✓ installed $n local hook(s) to $hooks_dst"
}

install_global() {
  mkdir -p "$GLOBAL_DIR"
  local n=0
  for src in "$HOOKS_SRC"/*; do
    cp "$src" "$GLOBAL_DIR/$(basename "$src")"
    chmod +x "$GLOBAL_DIR/$(basename "$src")"
    n=$((n + 1))
  done
  git config --global core.hooksPath "$GLOBAL_DIR"
  echo "✓ installed $n global hook(s) to $GLOBAL_DIR"
  echo "✓ git config --global core.hooksPath = $GLOBAL_DIR"
  echo "  → every repo now uses this hook directory"
}

case "${1:-}" in
  --global|-g)
    install_global
    ;;
  --help|-h)
    sed -n '2,26p' "$0"
    ;;
  "")
    install_local
    ;;
  *)
    echo "!! unknown argument: $1 (use --global or no args)" >&2
    exit 1
    ;;
esac
