---
name: behavior-first-testing
description: "Behavior-first testing for business applications. Acceptance scenarios are written and approved before the code, as actor-first Given/When/Then that users and PMs can read. No mocks: real Postgres/Redis, contract-verified fakes for external systems, fake clocks instead of sleeps. End-to-end tests run from the user's entry point to a visible result, with tiered and change-based test runs. Branch coverage is measured with every gap explained, and the rules are enforced by a bundled linter. Use this skill whenever you write, change, review, fix or delete tests in a product or business codebase; implement a feature or bug fix that should be tested; set up test infrastructure, CI or coverage; or a test breaks after a refactor. That includes when the user only says 'add tests', 'fix the failing test' or 'why is CI red'."
---

# Behavior-First Testing

Tests in a business application exist to prove that the business rules hold: an order is charged once, a
refund never exceeds what was paid, an invoice becomes overdue on the right day. A test earns its place when it
fails because a rule broke, and stays green while the rules hold, including through a refactor.

Tests written by an AI after the code tend to miss that in predictable ways. They restate the implementation
that was just written. They mock whatever is in the way. They sleep and hope. They pile up to reach a coverage
number. The six rules below exist to stop exactly that. Each comes with its reason, so you can apply the intent
in situations the rules don't name.

## The six rules

### 1. Test behavior, not implementation

Assert what a user or a downstream system can observe: the HTTP response, what a later read returns, the page
the user sees, the charge the payment fake recorded, the message in the fake outbox. Do not assert which
functions ran, how many times, in what order, or what private state looks like.

The litmus test: *if the business rule stays the same and someone refactors the code, does this test still
pass?* If a refactor would break it, it is testing the implementation.

```python
# Implementation: breaks when charge() is renamed, batched, or moved behind a queue
gateway.charge.assert_called_once()

# Behavior: "submitting the same order twice charges the customer once"
client.post("/orders", json=order, headers={"Idempotency-Key": "k1"})
client.post("/orders", json=order, headers={"Idempotency-Key": "k1"})
assert len(payments.charges_for(customer_id)) == 1
```

Assert exact values. Money is compared as `Decimal` or integer cents, never with `approx`: a one-cent
difference is a bug report from a customer. A test that only checks `response.ok`, or only that no exception
was raised, is not asserting behavior.

### 2. Scenarios first, approved by a human

Before writing implementation code, write the acceptance scenarios for the change and get them approved.

- **Names start with the actor.** Use `test_user_is_charged_once_when_submitting_the_same_order_twice` or
  `test("user sees the order confirmation", ...)`. The default actor is `user`. A project can add others (`admin`,
  `system` for scheduled jobs) in `.behavior-testing.toml`, but should do so deliberately.
- **Every scenario states Given / When / Then** in domain language a user or PM can follow. Use the docstring in
  Python, `// Given` comments or `test.step("Given ...")` in TypeScript, or steps in a `.feature` file.
- **A human approves them before you implement.** Show the list, then wait. When the user has already given
  acceptance criteria, use them as they are and show how you mapped them.

Why: a scenario written after the code describes the code, not the requirement. An AI will happily write tests
that prove its own implementation right. Writing scenarios first, and having a person agree to them, makes the
tests evidence about the requirement instead.

Once approved, a scenario is the contract. When it fails, change the code, not the scenario. If you believe a
scenario is wrong, stop and say so: show the scenario, the change you propose, and why. Never loosen an
assertion, widen a tolerance, or add a skip to get to green. When the guard hook is installed, it blocks edits to
committed scenarios; adding new scenarios stays allowed.

Details, examples and a checklist for finding the scenarios that matter: `references/scenarios.md`.

### 3. No mocks

- **Infrastructure you run:** use the real thing (PostgreSQL, Redis, queues, the filesystem), isolated per test.
- **External systems you don't run** (payment processor, SMS, tax or shipping APIs): use a fake that implements the same
  interface as your client. Back it with a contract test that runs the same assertions against the real
  sandbox, so the fake cannot drift from reality.
- **Time:** inject a clock and control it from the test. Never sleep; wait for an observable condition or advance
  the fake clock.
- **Randomness and IDs:** seed them or inject them.

Why: a mock encodes your assumption about a dependency, so the test proves only that the code agrees with your
assumption. A real dependency, or a fake verified against the real one, proves the code works with what it will
meet in production. AI assistants reach for mocks by reflex because they make any test easy to write. That ease
is the problem.

Passing a fake in (a constructor argument, a FastAPI `dependency_overrides` entry) is dependency injection and
is fine. Patching a module attribute, `vi.mock(...)`, and asserting on a mock's calls are not.

Isolation strategies, the fake-plus-contract pattern, failure injection and clocks: `references/real-dependencies.md`.

### 4. End-to-end by default

The default test enters where the user enters and checks what the user sees.

- **Entry points:** an HTTP endpoint, the UI, a message consumer, a CLI.
- **Visible results:** the response, the rendered page, a subsequent read, the fake outbox.

Only systems you don't run are faked. For a backend, an in-process HTTP client (ASGI `TestClient`,
`supertest`) against a real database counts as end-to-end. Browser tests (Playwright) cover the critical user
journeys against the real backend.

Drop to a lower level only when the scenario level is the wrong tool. The typical case is large combinations of a
pure rule: pricing tables, tax, date arithmetic, parsers. Say why in the module docstring. Even then, test
through the module's public API.

As the suite grows, split it into tiers and select by change. While iterating, run the tests affected by the
change. Before handing off, run every tier the change could affect. Run everything on the main branch and nightly.
Never let change-based selection be the only run. Tiers, selection tools and CI layout:
`references/e2e-and-tiers.md`.

### 5. Coverage last, and every gap explained

Once rules 1–4 are in place, hold branch coverage at **90%** or above. The number is a smoke alarm, not the goal.
What matters is why the rest is untested. For each uncovered branch in code you touched, especially failure
paths, do one of these:

- Add a scenario for it. This is the default for failure paths.
- Delete it, if it is unreachable.
- Write down why it is acceptable. Use an exclusion with a reason, which the linter requires.

The failure paths that matter most are declines, timeouts, conflicts, validation, permissions and partial failure.

Never write a test whose only purpose is to execute lines. A test with no meaningful assertion is worse than a
gap, because it hides the gap. `scripts/bft.py gaps` turns a coverage report into a table of gaps, with failure
paths first, for you to explain. Details: `references/coverage.md`.

### 6. Enforce the rules with a linter

`scripts/bft.py` (this skill's directory; standard library only, Python 3.9+) checks what can be checked
mechanically:

| Rule | Catches |
|---|---|
| BFT001 | mocks: `unittest.mock`, `mocker`, `monkeypatch.setattr`, HTTP stub libraries, `vi.mock`/`jest.mock`/`vi.fn`/`spyOn`, `sinon`, `nock`, `msw`, Playwright `page.route` |
| BFT002 | sleeps: `time.sleep`, `asyncio.sleep(n)`, `setTimeout`, `waitForTimeout`, `cy.wait(ms)` |
| BFT003 | reading the real clock in tests: `datetime.now()`, `date.today()`, `Date.now()`, `new Date()` |
| BFT004 | call assertions: `assert_called*`, `call_count`, `toHaveBeenCalled*`, `.mock.calls` |
| BFT005 | a scenario name that does not start with an actor |
| BFT006 | a scenario without Given, When and Then, in that order |
| BFT007 | skipped or focused tests (`skip`, `skipif`, `xfail`, `.only`, `.skip`, `fixme`) |
| BFT008 | coverage exclusions (`pragma: no cover`, `istanbul`/`c8`/`v8 ignore`) without a reason |
| BFT010 | a new test that already passes on the base branch (`red-on-base`) |

Commands (run from the repository root):

```bash
python3 <skill-dir>/scripts/bft.py lint [--changed-since origin/main]   # the rules above
python3 <skill-dir>/scripts/bft.py scenarios                             # scenarios as Given/When/Then, for review
python3 <skill-dir>/scripts/bft.py red-on-base                           # new tests must fail on the base branch
python3 <skill-dir>/scripts/bft.py gaps coverage.xml [--fail-under 90]   # explainable coverage gaps
python3 <skill-dir>/scripts/bft.py init                                  # starter .behavior-testing.toml
```

`red-on-base` enforces rule 2 mechanically. It runs the branch's new tests against the base branch's code, where
each one must fail. A new test that already passes there was fitted to the code, or tests behavior that already
existed.

When a rule genuinely does not apply, allow it on that line with a reason a reviewer can judge:
`# bft: allow BFT002 -- polling interval inside wait_until(); the wait ends on the condition`. An allowance
without a reason is itself a violation (BFT000). List every allowance you add in your report. Some rules the
linter cannot see (a test asserting private state, weak assertions, vague scenario language), so they stay your
responsibility.

For native ESLint/ruff rules, pre-commit, CI, the guard hook and CODEOWNERS: `references/enforcement.md`.

## Workflow: a feature or a bug fix

1. **Understand the behavior.** Read the requirement, the project's rules docs, and the existing scenarios
   (`bft.py scenarios`). Check `.behavior-testing.toml` for actors and folders.
2. **Draft the scenarios.** Cover the happy path, then the failure paths and edges, using the checklist in
   `references/scenarios.md`. For a bug, the first scenario reproduces the bug from the user's side.
3. **Get approval.** Show the scenario titles with their Given/When/Then in plain language. Wait for the human.
   If they told you to proceed without review, say that in your report.
4. **Write the scenario tests and watch them fail.** They must fail because the behavior is missing, not because
   of a typo, a broken fixture or a missing import. Read the failure message.
5. **Implement until green.** Do not edit approved scenarios. Add lower-level tests only where rule 4 allows.
6. **Run the right tests.** Run the affected tier while iterating, then every tier the change touches. Fix flakiness
   at its cause (time, ordering, shared data, async waits), never with retries or sleeps.
7. **Check coverage and explain gaps.** Run branch coverage and `bft.py gaps --changed-since <base>`. Add scenarios for
   failure paths that matter, and write reasons for the rest.
8. **Lint.** Run `bft.py lint --changed-since <base>`, and `bft.py red-on-base` when on a branch.
9. **Report back** in the format below.

Legacy codebases: adopt incrementally. Lint with `--changed-since` so new work follows the rules. Don't rewrite
old tests wholesale unless asked; fix old tests you touch anyway.

## When a test fails or breaks

| Situation | What to do |
|---|---|
| An approved scenario fails after your change | The code is wrong. Fix the code. |
| You believe an approved scenario is wrong | Stop. Show the scenario, the proposed change and the reason, and ask. |
| Behavior is unchanged but a test broke in a refactor | The test was coupled to implementation. Rewrite it to assert behavior, and mention it. |
| A test fails intermittently | Find the nondeterminism: real clock, test order, shared rows, unawaited work, fixed waits. Don't retry, sleep or skip. |
| A test needs an environment you don't have (a running server, sandbox credentials) | Say it did not run. Don't report it as passing, and don't turn it into a silent skip. |

## Reviewing tests

When asked to review tests or a PR:

1. Run `bft.py lint` (with `--changed-since` for a PR) and `red-on-base`.
2. Judge what the linter cannot see:
   - Does each test assert behavior, rather than private fields, SQL, log lines or snapshots of everything?
   - Are the scenarios in domain language?
   - Are failure paths covered?
   - Are the assertions exact?
   - Do tests share mutable data or depend on their order?
   - Is any fake missing its contract test?

## Report format

End any task that touched tests with:

```
Scenarios: <added / changed, by title; who approved them>
Ran: <commands and results, e.g. "tier: scenarios, 142 passed">
Not run: <what and why, e.g. "contract tests: no sandbox credentials">
Coverage: <branch % overall and for changed files; gaps left, each with its reason>
Allowances added: <file:line, rule, reason>, or "none"
```

## Reference files

- `references/scenarios.md`: writing scenarios in pytest, Vitest/Jest, Playwright and Gherkin; the scenario
  checklist; the approval flow. Read before drafting scenarios.
- `references/real-dependencies.md`: database and Redis isolation, fakes with contract tests, failure injection,
  clocks, randomness. Read before creating fixtures or faking anything.
- `references/e2e-and-tiers.md`: what counts as end-to-end, choosing a test level, tiers, change-based selection,
  CI layout, flaky tests, silent skips.
- `references/coverage.md`: branch coverage configuration, the gap report, padding smells, optional mutation
  testing.
- `references/enforcement.md`: `bft.py` and its config, native lint configs, pre-commit, GitHub Actions, the
  guard hook, CODEOWNERS.
- `references/characterization.md`: pinning a legacy system's behavior with historical data before replacing
  it: exact comparison, pinned differences with reasons, no tolerances.
