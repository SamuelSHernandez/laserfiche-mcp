#!/usr/bin/env bash
# ship.sh — move local commits on main onto a PR branch, push, wait for CI, squash-merge.
#
# Usage:
#   ./scripts/ship.sh
#   ./scripts/ship.sh "Optional PR title override"
#
# Workflow:
#   1. You commit normally on main (one or more commits).
#   2. ./scripts/ship.sh moves those commits to a branch, opens a PR,
#      waits for required checks, then squash-merges and deletes the branch.
#   3. You end up back on main, tracking origin/main, with the new commit pulled.
#
# Why this exists: the "main-protection" ruleset requires 4 Python-matrix CI
# checks before commits land on main. Direct pushes to main from this account
# are blocked (admin bypass is restricted to PR merges). This script is the
# fast path: it does the branch/push/PR/merge dance with one command.

set -euo pipefail

# --- Pre-flight -------------------------------------------------------------

command -v gh >/dev/null || { echo "error: gh CLI not found (brew install gh)" >&2; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "error: gh not authenticated (gh auth login)" >&2; exit 1; }

current_branch=$(git branch --show-current)
if [[ "$current_branch" != "main" ]]; then
    echo "error: not on main (currently on '$current_branch')" >&2
    exit 1
fi

if ! git diff --cached --quiet || ! git diff --quiet; then
    echo "error: working tree has uncommitted changes — commit first, then ship" >&2
    git status --short >&2
    exit 1
fi

git fetch origin main --quiet

ahead=$(git rev-list --count origin/main..HEAD)
if [[ "$ahead" -eq 0 ]]; then
    echo "error: no commits ahead of origin/main — nothing to ship" >&2
    exit 1
fi

# --- Derive PR title + branch name -----------------------------------------

# Title: first arg, or first commit subject.
title="${1:-$(git log --format=%s "origin/main..HEAD" --reverse | head -1)}"

# Slug: lowercase, non-alphanum → '-', collapse runs, trim, cap at 50 chars.
slug=$(printf '%s' "$title" \
    | tr '[:upper:]' '[:lower:]' \
    | sed -E 's/[^a-z0-9]+/-/g; s/^-+|-+$//g' \
    | cut -c1-50 \
    | sed -E 's/-+$//')
branch="ship/${slug}"

# If branch name collides (local or remote), append a timestamp.
if git rev-parse --verify "$branch" >/dev/null 2>&1 \
   || git ls-remote --heads origin "$branch" | grep -q .; then
    branch="${branch}-$(date +%s)"
fi

echo "→ shipping $ahead commit(s) as PR on branch '$branch'"

# --- Move commits to branch, reset main ------------------------------------

# Save HEAD, create branch, reset main back to origin/main.
# Safe: the commits live on $branch (and in the reflog) before we touch main.
saved_head=$(git rev-parse HEAD)
git branch "$branch" "$saved_head"
git reset --hard origin/main --quiet
git checkout "$branch" --quiet

# --- Push + open PR --------------------------------------------------------

git push -u origin "$branch" --quiet

pr_url=$(gh pr create --fill --title "$title" --base main --head "$branch")
echo "→ PR opened: $pr_url"

# --- Wait for required checks ----------------------------------------------

echo "→ waiting for required checks…"
if ! gh pr checks "$pr_url" --watch --required; then
    echo "error: required checks failed — PR left open for inspection" >&2
    git checkout main --quiet
    exit 1
fi

# --- Squash-merge + cleanup ------------------------------------------------

gh pr merge "$pr_url" --squash --delete-branch
echo "→ merged + branch deleted"

git checkout main --quiet
git pull --ff-only origin main --quiet

echo "✓ shipped: $pr_url"
