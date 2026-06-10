# AGENTS.md

Guidance for coding agents in this directory.

## Scope
- Applies to `/home/arnab/Projects/scripts` and children.
- This repo is a personal script collection: mostly executable shell/Python utilities plus a few design/product docs for `agent-usage`.

## Style
- Keep scripts small, direct, and dependency-light.
- Preserve executable scripts' shebangs and executable bits.
- Prefer POSIX shell where existing script uses sh; use Bash only when already required.
- For Python, keep stdlib-first unless the script already depends on third-party packages.
- Avoid broad rewrites of unrelated scripts.

## Safety
- Many scripts touch local desktop/system state. Read target script before changing behavior.
- Do not run destructive commands, installers, package managers, or scripts with side effects unless explicitly asked.
- When testing, prefer dry-run-ish commands, help flags, syntax checks, and isolated temp dirs.

## Testing
- Shell: run `bash -n <script>` for Bash scripts when applicable.
- Python: run `python -m py_compile <file>` for changed Python files.
- For `agent-usage` / `agent-context-compare.py`, use local sample/temp data when possible; avoid modifying real session history.

## UI/design notes
- `PRODUCT.md` and `DESIGN.md` describe `agent-usage` report direction.
- Keep the report dense, tactical, standalone, and local-first.
- Avoid generic SaaS cards, purple gradients, terminal cosplay, decorative glass, and external assets.
