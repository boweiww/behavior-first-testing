# Branch coverage and explained gaps

Coverage comes after rules 1–4. Once scenarios, real dependencies and end-to-end tests are in place, coverage
shows which behavior nobody has specified yet. Measure **branches**, not just lines: `if card_declined:` counts
as covered by line coverage even when only the happy path ever ran.

## Configure

**coverage.py / pytest-cov**

```toml
# pyproject.toml
[tool.coverage.run]
branch = true
source = ["app"]

[tool.coverage.report]
fail_under = 90
show_missing = true
skip_covered = true
exclude_also = [
  "if TYPE_CHECKING:",          # import-only blocks for type checkers
  "raise NotImplementedError",  # abstract methods
  "@overload",
]
```

```bash
pytest --cov --cov-branch --cov-report=term-missing --cov-report=xml   # writes coverage.xml
```

**Vitest**

```ts
// vitest.config.ts
export default defineConfig({
  test: {
    coverage: { provider: "v8", reporter: ["text", "cobertura"], thresholds: { branches: 90 } },
  },
});
// writes coverage/cobertura-coverage.xml
```

**Jest:** `coverageReporters: ["text", "cobertura"]`, `coverageThreshold: { global: { branches: 90 } }`.

**Coverage from a separately running server** (UI end-to-end): start the server under `coverage run
--parallel-mode` (or with `NODE_V8_COVERAGE` / `c8`), stop it after the run, then `coverage combine`. Otherwise
the end-to-end tier contributes nothing to the number.

## The gap report

```bash
python3 <skill-dir>/scripts/bft.py gaps coverage.xml --changed-since origin/main --fail-under 90
```

This prints a Markdown table. It lists every uncovered line range and partially taken branch in the files you
changed. Failure paths (raise, except, error, reject, 4xx/5xx) come first, and a file no test ever loaded gets a
single row. The last column is empty. Fill it in for each row with one of these outcomes:

| Outcome | When | What to do |
|---|---|---|
| **Add a scenario** | A behavior someone could hit: a decline, a validation error, a conflict, a permission check | Write it the same way as any scenario: actor-first, Given/When/Then. This is the default for failure paths. |
| **Delete the code** | Unreachable: a branch the types or the database constraints make impossible | Remove it, and mention it |
| **Accept, with a reason** | Reachable, but not worth an automated test (a crash handler that only logs), or covered by a tier that did not run here (contract, nightly) | Say so in the report. Optionally exclude it with a reason: `# pragma: no cover -- only reachable when the sandbox is down; covered by the nightly contract run` |

Put the filled-in table, or a summary of it, in your report. For failure paths, "no time" is not a reason.

To gate only the changed lines in CI, `diff-cover coverage.xml --compare-branch=origin/main --fail-under=90` works
too (it measures lines, not branches).

## Padding smells

These raise the number without testing anything. Don't write them, and flag them in review:

- A test with no assertion, or one that asserts only that no exception was raised.
- Calling private functions directly to reach a branch, instead of reaching it the way a user would.
- Snapshot assertions of whole responses or pages, which pass once written and get re-approved without reading.
- A test per getter, a constructor test, or a test that `__repr__` returns a string.
- Asserting on the mock or fake you just configured, rather than on the system's response to it.
- Parametrizing over many values of the same branch to look thorough.

## Optional: mutation testing

When you suspect coverage is padded, mutation testing measures whether the tests notice changes to the code.
Use `mutmut` or `cosmic-ray` for Python, and Stryker for JavaScript and TypeScript. It is slow, so run it nightly,
on core domain code only (pricing, billing, permissions). Treat surviving mutants like coverage gaps: add an
assertion that would catch them, or explain why not.
