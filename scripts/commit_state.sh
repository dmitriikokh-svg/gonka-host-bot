#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -lt 2 ]]; then
  echo "Usage: $0 <commit-message> <state-path> [state-path ...]" >&2
  exit 2
fi

commit_message="$1"
shift

git config user.name "gonka-bot"
git config user.email "gonka-bot@users.noreply.github.com"

for path in "$@"; do
  if [[ -e "$path" ]]; then
    git add -- "$path"
  fi
done

if git diff --staged --quiet; then
  echo "State did not change. Nothing to commit."
  exit 0
fi

git commit -m "$commit_message"

for attempt in 1 2 3; do
  if git push origin HEAD:main; then
    exit 0
  fi

  if [[ "$attempt" -lt 3 ]]; then
    echo "Push rejected; syncing with origin/main (attempt $attempt/3)."
    GIT_EDITOR=true git pull --rebase origin main
  fi
done

echo "Could not push state after 3 attempts." >&2
exit 1
