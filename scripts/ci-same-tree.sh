#!/bin/sh
# ci-same-tree.sh: answer whether a green pull_request run already tested
# HEAD's tree, so main pushes skip pure re-verification (same lookup as
# blockfield-mod release.yml). Prints "true" or "false".
set -eu
workflow="${1:?usage: ci-same-tree.sh <workflow-file>}"
repo="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
tree=$(git rev-parse 'HEAD^{tree}')
tested=false
for sha in $(gh api "repos/${repo}/actions/workflows/${workflow}/runs?status=success&per_page=30" --jq '.workflow_runs[].head_sha'); do
    t=$(gh api "repos/${repo}/git/commits/${sha}" --jq '.tree.sha')
    if [ "$t" = "$tree" ]; then
        tested=true
        break
    fi
done
# PR runs test the merge ref, whose tree differs from head_sha when the PR was
# behind main; those runs record tested-tree-<tree> artifacts.
if [ "$tested" != true ]; then
    for id in $(gh api "repos/${repo}/actions/workflows/${workflow}/runs?event=pull_request&status=success&per_page=20" --jq '.workflow_runs[].id' || true); do
        if gh api "repos/${repo}/actions/runs/${id}/artifacts?per_page=100" --jq '.artifacts[].name' 2>/dev/null | grep -qx "tested-tree-${tree}"; then
            tested=true
            break
        fi
    done
fi
echo "Same tree ${tree} already tested: ${tested}." >&2
echo "$tested"
