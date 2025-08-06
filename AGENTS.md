# AGENTS

## Scope
Applies to the entire repository.

## Code style
- Use 4 spaces for indentation in both Python and shell scripts.
- In Python, follow PEP 8 conventions and prefer double quotes for strings.
- Keep shell scripts `#!/usr/bin/env bash` with `set -euo pipefail` near the top.
- Write concise, well‑commented code.

## Testing
- When modifying `ptzpad.py`, run `python -m py_compile ptzpad.py`.
- When modifying `install.sh`, run `bash -n install.sh`.
- Ensure commands complete without errors and capture their output in the PR message.

## Documentation
- Update README or inline comments when behavior changes.

## Git conventions
- Use descriptive, imperative commit messages (e.g., `feat: add new option`).
- Keep the worktree clean before committing.
