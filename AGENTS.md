# Agent instructions

## Ruff

After changing Python code, run Ruff and fix every reported issue before considering the work complete.

```bash
uv run ruff check --fix app tests
uv run ruff check app tests
```

Do not report the task done while `ruff check` still fails. Apply safe auto-fixes first, then resolve remaining diagnostics by changing the code or adding a justified `# noqa` (for example worker-boundary `except Exception`). Do not disable rules globally just to get a clean run.
