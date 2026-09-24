# Enforcement: bft.py, native linters, CI, guard hook

Rules that live only in a document get skipped under deadline pressure, and an AI assistant forgets them between
sessions. Put as many as possible where a machine checks them.

## Contents
- bft.py
- .behavior-testing.toml
- Allowances
- Native linters (ruff, ESLint)
- pre-commit
- GitHub Actions
- The guard hook
- CODEOWNERS
- What stays with the reviewer

## bft.py

`scripts/bft.py` in this skill's directory uses only the Python standard library and runs on Python 3.9+. For
CI, copy it into the repository (for example `tools/bft.py`) so builds don't depend on anyone's Claude Code
install. Update the copy deliberately, like any other pinned tool.

| Command | Does | Exit code |
|---|---|---|
| `lint [PATH...] [--changed-since REF] [--format github]` | Checks BFT001–BFT008 | 1 on any problem |
| `scenarios [PATH...]` | Prints scenarios as Markdown with Given/When/Then | 0 |
| `red-on-base [--base REF] [--command CMD] [--link PATH] [--with-new-files] [--allow-not-run]` | Runs new tests against the base branch in a temporary git worktree; each must fail | 1 if one passes (BFT010) or could not run |
| `gaps COVERAGE_XML [--changed-since REF] [--fail-under PCT]` | Prints the coverage-gap table | 1 when under the bar |
| `guard` | Claude Code PreToolUse hook (event on stdin) | 2 blocks the edit |
| `init [--force]` | Writes a starter config with detected test runners | 0 |
| `rules` | Lists rule ids | 0 |

`--changed-since REF` compares against the merge-base with REF and includes uncommitted and untracked files. It
is how a legacy codebase adopts the rules: new and changed files must comply from day one, and old files are
fixed when touched.

### How red-on-base works

1. It finds test files changed since the merge-base, and within them, the tests that did not exist at the
   merge-base.
2. It checks out the merge-base in a temporary `git worktree`, copies the changed test and support files in, and
   symlinks `.venv`, `venv` and `node_modules` directories from your checkout. Ignored files the tests need, such
   as `.env`, are not there; lend them with `--link .env` or list them in `link`.
3. It runs each runner's command on the new test files and reads the JUnit XML it writes.
4. A test for the new behavior must fail there. A test file or a `conftest.py` that cannot load on base (it
   imports something the branch adds) counts as failing, but that is weak evidence; see step 4 of the workflow. A test that passes is reported as BFT010, unless an allowance marks it as a guard:
   an exception or boundary of the new rule, or a characterization test. A test that could not run fails the
   check too, unless `--allow-not-run` is given.
5. With `--with-new-files`, the tests run a second time with the files the branch adds copied in (new modules,
   not modifications). A test that still fails is labelled "fails on its own assertion": strong evidence that
   it depends on the changed behavior. A test that now passes is labelled "fails only while the new files are
   missing": it tests new code on its own. That is fine for a unit test of a new module, but a scenario should
   exercise the change to existing code as well.

Your working tree is never modified, and the worktree is removed afterwards.

## .behavior-testing.toml

At the repository root. Every key is optional; `bft.py init` writes one with your runners detected.

```toml
# Test files and test support code (fixtures, fakes, helpers): BFT001-BFT007 apply.
test_globs = ["**/test_*.py", "**/*_test.py", "**/conftest.py", "**/tests/**", "**/test/**",
              "**/__tests__/**", "**/e2e/**", "**/*.test.*", "**/*.spec.*", "**/*.feature"]

# Acceptance scenarios: actor-first names, Given/When/Then, protected by the guard once committed.
scenario_globs = ["**/tests/acceptance/**", "**/tests/e2e/**", "**/e2e/**", "**/*.feature"]

# Scenario names start with one of these.
actors = ["user"]

# Checked for coverage exclusions without a reason (BFT008).
source_globs = ["**/*.py", "**/*.ts", "**/*.tsx", "**/*.js", "**/*.jsx"]
exclude_globs = ["**/node_modules/**", "**/.venv/**", "**/dist/**", "**/build/**"]

[guard]
enabled = true

[red_on_base]
base = "origin/main"
link = []            # extra paths to borrow from your checkout, e.g. ["backend/.env.test"]
copy_globs = []      # extra changed files to copy into the base checkout

[red_on_base.runners.backend]
globs = ["backend/**"]
cwd = "backend"
command = ".venv/bin/python -m pytest {files} -q -p no:cacheprovider --continue-on-collection-errors --junitxml={junit}"

[red_on_base.runners.frontend]
globs = ["frontend/**"]
cwd = "frontend"
command = "npx vitest run {files} --reporter=junit --outputFile={junit}"
```

`{files}` becomes the new test files (absolute paths inside the base checkout). `{junit}` becomes the report path.
Keep `--continue-on-collection-errors` for pytest: a new test file that imports something only the branch adds
cannot load on base, and without the flag pytest stops before running the other files.
For Playwright use `PLAYWRIGHT_JUNIT_OUTPUT_NAME={junit} npx playwright test {files} --reporter=junit`. For Jest
use `JEST_JUNIT_OUTPUT_FILE={junit} npx jest {files} --reporters=jest-junit` (needs `jest-junit`).

## Allowances

```python
# bft: allow BFT002 -- polling interval inside wait_until(); the wait ends on the condition
time.sleep(interval)

time.sleep(interval)  # bft: allow BFT002 -- same allowance, on the line itself

# bft: allow-file BFT001 -- this module is the respx-based fake; its contract test is tests/contract/test_tax_api.py
```

- A standalone allowance comment covers the next code line. A trailing one covers its own line. `allow-file`
  covers the whole file.
- A reason of at least 10 characters is required. An allowance without one is BFT000 and allows nothing.
- For BFT005, BFT006 and BFT010, put the allowance on the line above the test (or above its decorators).
- Allowances are a record for reviewers. List every one you add in your report.

## Native linters

`bft.py` covers everything. When a project already runs ruff or ESLint in the editor, these configurations flag
the most common cases as you type.

**ruff** (a `tests/ruff.toml` that extends the root config):

```toml
extend = "../pyproject.toml"

[lint]
extend-select = ["TID251"]

[lint.flake8-tidy-imports.banned-api]
"unittest.mock".msg = "No mocks: use the real dependency, or a fake verified by a contract test."
"pytest_mock".msg = "No mocks: use the real dependency, or a fake verified by a contract test."
"responses".msg = "HTTP stubs are unverified fakes: put a fake behind your own client interface."
"time.sleep".msg = "Wait for a condition, or advance a fake clock."
"datetime.datetime.now".msg = "Inject a clock and set it from the test."
"datetime.date.today".msg = "Inject a clock and set it from the test."
```

**ESLint** (flat config, test files only):

```js
// eslint.config.js (excerpt)
const noMock = "No mocks: use the real dependency, or a fake verified by a contract test.";
export default [
  {
    files: ["**/*.test.{ts,tsx,js}", "**/*.spec.{ts,tsx,js}", "e2e/**/*.{ts,js}"],
    rules: {
      "no-restricted-imports": ["error", {
        paths: ["sinon", "nock", "msw", "testdouble", "jest-mock-extended", "vitest-mock-extended"]
          .map((name) => ({ name, message: noMock })),
        patterns: [{ group: ["msw/*"], message: noMock }],
      }],
      "no-restricted-properties": ["error",
        ...["mock", "doMock", "spyOn", "fn", "stubGlobal"].flatMap((property) => [
          { object: "vi", property, message: noMock },
          { object: "jest", property, message: noMock },
        ]),
        { object: "page", property: "route", message: "UI end-to-end tests run against the real backend." },
        { object: "page", property: "waitForTimeout", message: "Wait for a condition, not a duration." },
        { object: "Date", property: "now", message: "Inject a clock, or use fake timers." },
        ...["it", "test", "describe"].flatMap((object) => [
          { object, property: "only", message: "A focused test hides the rest of the suite." },
          { object, property: "skip", message: "A skipped test passes silently." },
        ]),
      ],
      "no-restricted-globals": ["error", { name: "setTimeout", message: "Wait for a condition, or advance fake timers." }],
      "no-restricted-syntax": ["error",
        { selector: "NewExpression[callee.name='Date'][arguments.length=0]", message: "Inject a clock, or use fake timers." },
        { selector: "CallExpression[callee.property.name=/^(toHaveBeenCalled|toBeCalled)/]",
          message: "Assert the outcome a user or downstream system can observe, not the calls." },
      ],
    },
  },
];
```

Neither linter can check scenario naming, Given/When/Then, reasons on coverage exclusions or red-on-base. Keep
`bft.py` in CI regardless.

## pre-commit

```yaml
# .pre-commit-config.yaml
repos:
  - repo: local
    hooks:
      - id: bft-lint
        name: behavior-first testing rules
        entry: python3 tools/bft.py lint --changed-since HEAD
        language: system
        pass_filenames: false
```

## GitHub Actions

`assets/github-actions.yml` is a starting workflow:

- **Pull requests:** lint, red-on-base, the fast tier with branch coverage, and the gap report in the job summary.
- **Main:** everything but contract tests.
- **Nightly:** contract tests.

Adjust the install steps and services (PostgreSQL, Redis) to the project. `actions/checkout` needs
`fetch-depth: 0` so the base branch is available to `--changed-since` and `red-on-base`.

## The guard hook

The guard stops Claude Code from rewriting committed scenarios to fit its code. It only acts in repositories that
contain `.behavior-testing.toml` with `[guard] enabled = true` (the default when the file exists).

It blocks an Edit, Write or MultiEdit that would change the text of a test that exists in `HEAD` inside
`scenario_globs`. It allows:

- new scenarios, even when they are added to an existing file;
- uncommitted draft scenarios;
- every other file.

The human can edit scenarios as usual, or start Claude Code with `BFT_UNLOCK_SCENARIOS=1` for a session where
scenarios are meant to change.

**Installed as a plugin:** the hook is registered automatically (`hooks/hooks.json` in the plugin).

**Installed by copying the skill:** add it to `.claude/settings.json` in the project:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit",
        "hooks": [{ "type": "command", "command": "python3 \"$CLAUDE_PROJECT_DIR\"/tools/bft.py guard" }]
      }
    ]
  }
}
```

The guard is a guardrail, not a security boundary. A shell command can still change files. The real gate is
review, which is what CODEOWNERS is for.

## CODEOWNERS

Require a product-side reviewer for scenario changes:

```
# .github/CODEOWNERS
/backend/tests/acceptance/   @your-org/product-owners
/e2e/                        @your-org/product-owners
```

Also attach `bft.py scenarios` output to pull requests, or diff it against the base branch. A behavior change
then shows up as a readable change in the scenarios.

## What stays with the reviewer

The linter cannot judge:

- whether an assertion checks behavior or private state;
- whether a scenario is written in domain language;
- whether the failure paths that matter are covered;
- whether a fake behaves like the real system (that is the contract test's job).

These are the review checklist in `SKILL.md`.
