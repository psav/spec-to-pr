#!/usr/bin/env bash
#
# Run spec-to-pr in a container with all required credentials and environment variables.
#
# The container workspace is always /workspace — the shared root where all repos live
# as siblings (e.g. /workspace/spec-to-pr, /workspace/rosa-regional-platform).
# Never mount only the spec-to-pr subdirectory; that causes cloned repos to land
# inside spec-to-pr's tree and confuses the committer.
#
# Usage:
#   ./run-spec.sh run path/to/spec.md
#   ./run-spec.sh generate "rebase PR #362 onto main" [-o output.md]

set -euo pipefail

SUBCOMMAND="${1:-}"

_common_args() {
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
    "$@"
}

WORKSPACE_ROOT="/workspace"
SPEC_TO_PR_DATA="${HOME}/.spec-to-pr"
mkdir -p "${SPEC_TO_PR_DATA}/sessions" "${SPEC_TO_PR_DATA}/conversations"

case "${SUBCOMMAND}" in
  run)
    SPEC_FILE="${2:-}"
    if [[ -z "$SPEC_FILE" || ! -f "$SPEC_FILE" ]]; then
      echo "Usage: $0 run <path-to-spec.md>"
      exit 1
    fi
    SPEC_FILE="$(realpath "$SPEC_FILE")"
    echo "Running spec-to-pr for: $SPEC_FILE"
    _common_args spec-to-pr run \
      --file "$SPEC_FILE" \
      --project-docs /workspace/spec-to-pr/CLAUDE.md
    ;;

  generate)
    TASK="${2:-}"
    OUTPUT="${4:-}"  # optional: ./run-spec.sh generate "task" -o output.md
    if [[ -z "$TASK" ]]; then
      echo "Usage: $0 generate \"task description\" [-o output.md]"
      exit 1
    fi
    # If -o flag provided, map output path into container
    OUTPUT_ARGS=()
    if [[ -n "$OUTPUT" ]]; then
      OUTPUT_ABS="$(realpath "$OUTPUT")"
      OUTPUT_ARGS=(--output "$OUTPUT_ABS")
    fi
    echo "Generating spec for: $TASK"
    _common_args spec-to-pr generate "$TASK" "${OUTPUT_ARGS[@]}"
    ;;

  *)
    # Legacy: bare spec file path (backwards compat)
    if [[ -n "$SUBCOMMAND" && -f "$SUBCOMMAND" ]]; then
      SPEC_FILE="$(realpath "$SUBCOMMAND")"
      echo "Running spec-to-pr for: $SPEC_FILE"
      _common_args spec-to-pr run \
        --file "$SPEC_FILE" \
        --project-docs /workspace/spec-to-pr/CLAUDE.md
    else
      echo "Usage:"
      echo "  $0 run <path-to-spec.md>"
      echo "  $0 generate \"task description\" [-o output.md]"
      exit 1
    fi
    ;;
esac
