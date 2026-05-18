#!/usr/bin/env bash
#
# Run spec-to-pr in a container with all required credentials and environment variables.
#
# The container workspace is always /workspace — the shared root where all repos live
# as siblings (e.g. /workspace/spec-to-pr, /workspace/rosa-regional-platform).
# Never mount only the spec-to-pr subdirectory; that causes cloned repos to land
# inside spec-to-pr's tree and confuses the committer.
#
# Usage: ./run-spec.sh path/to/spec.md

set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 <path-to-spec.md>"
    echo "Example: $0 /workspace/spec-to-pr/my-spec.md"
    exit 1
fi

SPEC_FILE="$1"

if [[ ! -f "$SPEC_FILE" ]]; then
    echo "Error: Spec file not found: $SPEC_FILE"
    exit 1
fi

# Convert to absolute path. The spec must live somewhere under /workspace so
# that the container sees it at the same path.
SPEC_FILE="$(realpath "$SPEC_FILE")"
WORKSPACE_ROOT="/workspace"

# Ensure spec-to-pr data directory exists on host (outside workspace)
SPEC_TO_PR_DATA="${HOME}/.spec-to-pr"
mkdir -p "${SPEC_TO_PR_DATA}/sessions"
mkdir -p "${SPEC_TO_PR_DATA}/conversations"

echo "Running spec-to-pr for: $SPEC_FILE"
echo "Workspace root: $WORKSPACE_ROOT"
echo "Data directory: $SPEC_TO_PR_DATA"
echo ""

podman run --rm \
  -v "${WORKSPACE_ROOT}":/workspace:z \
  -v "${SPEC_TO_PR_DATA}":/spec-to-pr-data:z \
  -v ~/.aws/credentials:/root/.aws/credentials:ro,z \
  -e "GITHUB_TOKEN=${GITHUB_TOKEN}" \
  -e "AWS_PROFILE=${AWS_PROFILE:-rrp-central}" \
  -e "ANTHROPIC_VERTEX_PROJECT_ID=${ANTHROPIC_VERTEX_PROJECT_ID}" \
  -e "CLOUD_ML_REGION=${CLOUD_ML_REGION}" \
  -e "CLAUDE_CODE_USE_VERTEX=${CLAUDE_CODE_USE_VERTEX}" \
  -e "CLAUDE_CODE_SKIP_VERTEX_AUTH=${CLAUDE_CODE_SKIP_VERTEX_AUTH}" \
  -e "HTTPS_PROXY=${HTTPS_PROXY}" \
  -e "HTTP_PROXY=${HTTP_PROXY}" \
  -e "https_proxy=${https_proxy}" \
  -e "http_proxy=${http_proxy}" \
  -e "AWS_CA_BUNDLE=${AWS_CA_BUNDLE:-}" \
  spec-to-pr run \
    --file "$SPEC_FILE" \
    --project-docs /workspace/spec-to-pr/CLAUDE.md
