#!/usr/bin/env bash
#
# Run spec-to-pr in a container with all required credentials and environment variables
#
# Usage: ./run-spec.sh path/to/spec.md

set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 <path-to-spec.md>"
    echo "Example: $0 .claude/specs/my-feature.md"
    exit 1
fi

SPEC_FILE="$1"

if [[ ! -f "$SPEC_FILE" ]]; then
    echo "Error: Spec file not found: $SPEC_FILE"
    exit 1
fi

# Convert to absolute path
SPEC_FILE="$(realpath "$SPEC_FILE")"
REPO_ROOT="$(git rev-parse --show-toplevel)"

# Ensure conversations directory exists
CONVERSATIONS_DIR="${REPO_ROOT}/conversations"
mkdir -p "${CONVERSATIONS_DIR}"

echo "Running spec-to-pr for: $SPEC_FILE"
echo "Repository root: $REPO_ROOT"
echo "Conversations: $CONVERSATIONS_DIR"
echo ""

podman run --rm \
  -v "${REPO_ROOT}":/workspace:z \
  -v "${CONVERSATIONS_DIR}":/conversations:z \
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
  -e "AWS_CA_BUNDLE=${AWS_CA_BUNDLE}" \
  spec-to-pr run \
    --file "/workspace/${SPEC_FILE#$REPO_ROOT/}" \
    --project-docs /workspace/CLAUDE.md
