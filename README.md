# Behavior-First Testing

A Claude Code skill (and plugin) for testing business applications. Tests prove business rules, not the code
that happens to implement them, and a linter keeps it that way.

AI assistants write tests fast, and the same failures show up again and again:

- **Tests that restate the code.** The assistant writes the implementation first, then a test that proves its
  own implementation is right.
- **Mocks by reflex.** It mocks whatever is in the way, so the test only confirms the code agrees with the
  mock.
- **Sleeps.** It adds `sleep(2)` and hopes the timing works out.
- **Coverage padding.** It keeps adding tests until a coverage number turns green.

This skill steers Claude away from all four. The bundled `bft.py` checks mechanically as much of that as can be
checked.

## The six rules

1. **Test behavior, not implementation.** "Submitting the same order twice charges once" instead of "`charge()`
   was called once". If the business rule is unchanged and a refactor breaks the test, the test was wrong.
2. **Scenarios first, approved by a human.** Acceptance scenarios come before the code. Names start with the
   actor (`user ...`) and use Given / When / Then, so users and PMs can read them. Approved scenarios are the
   contract, and the code changes to meet them, never the other way round.
3. **No mocks.** Use real PostgreSQL and Redis. External systems get fakes that are verified against the real
   sandbox by contract tests. Time comes from a fake clock, never from `sleep`.
4. **End-to-end by default.** Tests go from the user's entry point to a visible result. As the suite grows, it
   is split into tiers and selected by change, with full runs on main and nightly.
5. **Coverage last, gaps explained.** 90% branch coverage. What matters is *why* the rest is untested,
   especially failure paths. No tests written just to move the number.
6. **Rules as lint.** Everything above that a machine can check, it does.

## What's inside

```
skills/behavior-first-testing/
├── SKILL.md                    the rules, the workflow, the report format
├── references/                 loaded on demand
│   ├── scenarios.md            naming, Given/When/Then in pytest, Vitest, Playwright and Gherkin; scenario checklist
│   ├── real-dependencies.md    DB isolation, fakes + contract tests, failure injection, clocks
│   ├── e2e-and-tiers.md        choosing a level, tiers, change-based selection, CI, flaky tests, silent skips
│   ├── coverage.md             branch coverage setup, the gap report, padding smells
│   ├── enforcement.md          bft.py, native ruff/ESLint rules, pre-commit, CI, guard hook, CODEOWNERS
│   └── characterization.md     pinning a legacy system's behavior with historical data
├── scripts/bft.py              the linter and checks (standard library only, Python 3.9+)
└── assets/github-actions.yml   a starting CI workflow
hooks/hooks.json                the guard: Claude can't rewrite approved scenarios
```

## Install

As a plugin, which includes the guard hook. In Claude Code:

```
/plugin marketplace add boweiww/behavior-first-testing
/plugin install behavior-first-testing@behavior-first-testing
```

Or copy just the skill:

```bash
git clone https://github.com/boweiww/behavior-first-testing
cp -r behavior-first-testing/skills/behavior-first-testing ~/.claude/skills/        # every project
cp -r behavior-first-testing/skills/behavior-first-testing .claude/skills/          # one project
```

The skill triggers on its own whenever Claude writes, reviews or fixes tests. You can also ask for it by name.

## Set up a project

```bash
python3 path/to/bft.py init        # writes .behavior-testing.toml with your test runners detected
python3 path/to/bft.py lint        # see where the project stands
cp path/to/bft.py tools/bft.py     # vendor it for CI
```

`.behavior-testing.toml` is also what switches the guard hook on for that repository. See
[`references/enforcement.md`](skills/behavior-first-testing/references/enforcement.md) for the settings, CI
setup and hook installation without the plugin.

## bft.py

| Command | What it does |
|---|---|
| `lint [--changed-since REF]` | Checks the rules below. `--changed-since` lints only what a branch touched, which is how an existing codebase adopts the rules |
| `scenarios` | Prints every scenario with its Given/When/Then as Markdown, for a PM to review |
| `red-on-base` | Runs the branch's *new* tests against the base branch in a temporary worktree. Tests for the new behavior must fail there; a test that already passes was fitted to the code, unless it is marked as a guard for behavior that must not change |
| `gaps coverage.xml` | Turns a Cobertura report into a table of untested branches, failure paths first, each waiting for a reason |
| `guard` | Claude Code PreToolUse hook: blocks edits to committed scenarios, allows adding new ones |
| `init` | Writes a starter config |

| Rule | Catches |
|---|---|
| BFT001 | mocks: `unittest.mock`, `mocker`, `monkeypatch.setattr`, HTTP stubs (`responses`, `respx`, `httpx.MockTransport`), `vi.mock`/`jest.mock`/`vi.fn`/`spyOn`, `sinon`, `nock`, `msw`, Playwright `page.route` |
| BFT002 | sleeps: `time.sleep`, `asyncio.sleep(n)`, `setTimeout`, `waitForTimeout`, `cy.wait(ms)` |
| BFT003 | the real clock in tests: `datetime.now()`, `date.today()`, `Date.now()`, `new Date()` |
| BFT004 | call assertions: `assert_called*`, `call_count`, `toHaveBeenCalled*`, `.mock.calls` |
| BFT005 | a scenario name that doesn't start with an actor |
| BFT006 | a scenario without Given, When and Then, in order |
| BFT007 | skipped or focused tests |
| BFT008 | coverage exclusions without a reason |
| BFT010 | a new test that already passes on the base branch, unless marked as a guard |

When a rule genuinely doesn't apply, allow it with a reason a reviewer can judge:

```python
time.sleep(interval)  # bft: allow BFT002 -- polling interval inside wait_until(); the wait ends on the condition
```

An allowance without a reason is itself a violation.

Example run on a branch:

```
$ python3 tools/bft.py red-on-base
bft red-on-base: 2 new test(s), run against origin/main @ 81a6c44dd
  red      backend/tests/test_checkout.py::test_user_paying_cash_sees_the_total_rounded_to_the_nickel
  PASSES   backend/tests/test_checkout.py::test_user_paying_by_card_is_charged_to_the_cent  (already passes on base; it does not pin the new behavior: BFT010)
```

## FAQ

**No mocks at all?** Nothing you own gets mocked: no databases, caches, queues or your own modules. External
systems get fakes, which are working in-memory implementations of your own interface. Contract tests keep each
fake honest against the vendor's sandbox. Passing a fake in through dependency injection is fine. Patching a
module attribute is not.

**Are unit tests banned?** No. End-to-end scenarios are the default, but a pure rule with many combinations
(price tables, tax, date arithmetic) is better tested as a table of cases against its public function. The
skill asks for a sentence explaining why.

**Why must every name start with `user`?** A name that starts with who is acting reads as a requirement, not a
function name. `user` is the default; a project can add actors such as `admin` or `system` in its config.

**Does the guard stop a determined agent?** No. It is a guardrail against the common failure, where the
assistant edits the assertion to match its code. A shell command can still change files. The real gate is
review, and the docs show how to use CODEOWNERS for that.

## Requirements

Python 3.9+ and git. No Python packages are needed to run `bft.py`; `pytest` is needed to run its own tests.

## Contributing

```bash
python -m pip install pytest
python -m pytest tests -q
python skills/behavior-first-testing/scripts/bft.py lint   # this repository follows its own rules
```

The tests drive the real CLI against real temporary git repositories. There are no mocks, and every test is a
`user` scenario with Given / When / Then.

## License

MIT
