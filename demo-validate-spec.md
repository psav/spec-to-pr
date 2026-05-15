---
work_id: SPEC-VALIDATE
---
# Add `validate` subcommand to spec-to-pr CLI

## What to build

Add a `validate` subcommand to the `spec-to-pr` CLI
(`src/spec_to_pr/cli.py`) that checks whether a given spec file is valid
before running the full orchestrator.

## CLI interface

```
spec-to-pr validate --file PATH
```

## Behaviour

1. Read the file at `--file PATH`. If it doesn't exist, exit 1 with an
   error message.
2. Parse YAML frontmatter (if present). The frontmatter parser already
   exists in `src/spec_to_pr/models/work_item.py` — reuse `_parse_frontmatter`.
3. Print a checklist to stdout:

```
Validating: path/to/spec.md
  [OK] file readable
  [OK] work_id: SPEC-VALIDATE        (or [WARN] no work_id — will auto-generate)
  [OK] spec_content: 142 chars
```

4. Exit 0 if no errors, exit 1 if the file couldn't be read.

## Files to modify

- `src/spec_to_pr/cli.py` — add `validate` subparser and `_cmd_validate`
  function following the same pattern as the existing `_cmd_status`.

## Tests

Add a test file `tests/test_cli_validate.py` with at least:
- `test_validate_file_with_frontmatter` — exits 0, prints work_id
- `test_validate_file_without_frontmatter` — exits 0, prints WARN
- `test_validate_missing_file` — exits 1

Use `subprocess.run([".venv/bin/spec-to-pr", "validate", "--file", ...])` to
invoke the installed CLI so the tests are end-to-end.
