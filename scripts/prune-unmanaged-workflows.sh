#!/usr/bin/env bash
# Removes any GitHub Actions workflow files not in this fork's allowlist.
# Used during upstream sync to prevent two failures:
#   1. GITHUB_TOKEN refuses pushes that add/modify .github/workflows/*
#      without the workflow scope ("refusing to allow a GitHub App to
#      create or update workflow ... without 'workflows' permission").
#   2. Upstream-introduced workflows (e.g. ci-startup-check.yml) running
#      against fork-specific config they don't understand.
#
# Run with the working tree on a merge commit (or after a merge) — any
# unmanaged file is `git rm`'d and staged. Caller decides whether to
# amend the merge commit or create a follow-up commit.
set -euo pipefail

ALLOWED=(
    ".github/workflows/build.yml"
    ".github/workflows/release.yaml"
    ".github/workflows/sync-upstream.yml"
)

is_allowed() {
    local f=$1
    for a in "${ALLOWED[@]}"; do
        [ "$f" = "$a" ] && return 0
    done
    return 1
}

removed=0
while IFS= read -r f; do
    [ -z "$f" ] && continue
    if ! is_allowed "$f"; then
        echo "Pruning unmanaged workflow: $f"
        git rm -f "$f" >/dev/null
        removed=$((removed + 1))
    fi
done < <(git ls-files .github/workflows)

if [ "$removed" -gt 0 ]; then
    echo "Pruned $removed unmanaged workflow file(s)."
else
    echo "No unmanaged workflow files to prune."
fi
