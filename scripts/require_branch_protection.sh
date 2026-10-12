#!/usr/bin/env bash
# Make the CI gate a required status check on the deploy branch.
#
# MIRIAM deploys through AtlasFlow; the gate that must hold before a deploy is
# the CI pipeline (most importantly `capability-gate`). This script requires the
# pipeline's checks on the branch so a deploy cannot start from an unverified
# revision. See docs/PRODUCTION-READINESS.md.
#
# If the branch already has protection, only `required_status_checks` is
# PATCHed, so reviews / enforce-admins / restrictions are left as they are. If
# the branch is unprotected, a minimal protection is created (required checks
# only, no required reviews, enforce-admins off) — review that policy before
# running if you want more.
#
# Needs a token with `administration:write` (a classic PAT with `repo`, or a
# fine-grained token with "Administration: read and write").
#
# Usage: scripts/require_branch_protection.sh [owner/repo] [branch]
set -euo pipefail

repo="${1:-tobi-techy/MIRIAM}"
branch="${2:-main}"

# Exact check-run names from .github/workflows/ci.yml (matrix jobs include the
# Python version in their name).
read -r -d '' checks_json <<'JSON' || true
["test (3.11)", "test (3.12)", "trust-gates (3.12)", "capability-gate", "security-scan"]
JSON

echo "Current required checks on ${repo}@${branch}:"
if gh api "repos/${repo}/branches/${branch}/protection/required_status_checks" \
    --jq '.contexts' 2>/dev/null; then
  echo "Patching required_status_checks only."
  printf '{"strict": true, "contexts": %s}\n' "$checks_json" | \
    gh api -X PATCH \
      "repos/${repo}/branches/${branch}/protection/required_status_checks" \
      -H "Accept: application/vnd.github+json" --input - >/dev/null
else
  echo "  (branch is unprotected; creating minimal protection)"
  printf '{"required_status_checks": {"strict": true, "contexts": %s}, "enforce_admins": false, "required_pull_request_reviews": null, "restrictions": null}\n' \
    "$checks_json" | \
    gh api -X PUT "repos/${repo}/branches/${branch}/protection" \
      -H "Accept: application/vnd.github+json" --input - >/dev/null
fi

echo "Required checks on ${repo}@${branch} now:"
gh api "repos/${repo}/branches/${branch}/protection/required_status_checks" \
  --jq '.contexts'
