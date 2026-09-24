# End-to-end tests, tiers and selection

## Contents
- What counts as end-to-end
- Choosing a test level
- Tiers
- Change-based selection
- CI layout
- Silent skips
- Flaky tests

## What counts as end-to-end

A test is end-to-end for our purposes when it:

1. **Enters where the user enters.** That can be an HTTP endpoint, the UI, a message consumer, a CLI command or a
   scheduled job's entry function.
2. **Runs through the real stack.** That means real routing, validation, services, persistence and the real
   database and cache.
3. **Checks what the user or next system sees.** That can be the response body and status, the page, a subsequent
   read, the message published, or the fake outbox.

Only systems you don't run are replaced, by contract-verified fakes. For a backend service, an in-process HTTP
client (FastAPI/Starlette `TestClient`, `httpx.AsyncClient` with `ASGITransport`, `supertest`) against a real
database meets this bar. It is fast enough to be the default for nearly every business rule.

UI end-to-end tests (Playwright) drive a browser against the real backend. They are slower, so keep them for
critical journeys (sign-in, checkout, the core daily workflow) and for anything that only exists in the UI.
Never stub the backend with `page.route` in these tests; a stubbed backend makes it a component test with extra
steps.

## Choosing a test level

| What you are testing | Level |
|---|---|
| A business rule a user can observe (pricing on an order, refunds, permissions, state changes) | Scenario through the API: the default |
| A journey across several pages | UI scenario (Playwright); a few, not one per rule |
| Many combinations of a pure calculation (a price table, tax rules, date arithmetic, a parser) | Table-driven tests of the public function, plus a few scenarios proving it is wired in |
| Your adapter to an external system | Contract test: the fake and the sandbox run the same suite |
| A schema or data migration | Apply it to a copy of realistic data and assert on the data afterwards |
| Frontend behavior with no server round-trip (a form's validation, a calculator widget) | Component test with real rendering and user events (Testing Library). No `vi.mock` of modules |

When you go below the scenario level, say why in the module docstring. The linter does not require actor naming
outside scenario folders; the no-mock, no-sleep and no-clock rules still apply everywhere.

## Tiers

Tag tests by cost and by what they need, then run each tier where it pays off.

```toml
# pyproject.toml
[tool.pytest.ini_options]
addopts = "--strict-markers"
markers = [
  "ui: drives a browser against a running app",
  "slow: takes more than a few seconds; runs on main and nightly",
  "contract: runs against external sandboxes; nightly and on adapter changes",
]
```

```bash
pytest -m "not ui and not slow and not contract"   # the fast tier: every push
pytest -m "ui"                                     # UI journeys: main, and PRs touching the frontend
pytest -m "contract"                               # sandboxes: nightly
```

In Playwright, tag in the test details (`test("user ...", { tag: "@smoke" }, async ({ page }) => ...)`) and
select with `--grep @smoke`.

## Change-based selection

Run what the change could affect while iterating and on pull requests. Run everything on the main branch and
nightly, so selection mistakes are caught within a day.

| Stack | Tool |
|---|---|
| pytest | `pytest-testmon`: records which tests executed which code and reruns only the affected ones (`pytest --testmon`) |
| Vitest | `vitest related <changed files> --run`, or `vitest --changed origin/main` |
| Jest | `jest --changedSince=origin/main`, or `--findRelatedTests <files>` |
| Playwright | `npx playwright test --only-changed=origin/main` (1.46+) |
| Monorepo | `nx affected -t test`, `turbo run test --filter=...[origin/main]` |

Where no tool fits, map directories to tiers in CI. For example, changes under `frontend/` run the UI tier, and
changes under `backend/app/pricing/` run the pricing tables and the checkout scenarios.

When you report results, say what was selected and what was not run.

## CI layout

| Trigger | Runs | Target time |
|---|---|---|
| Pull request | `bft.py lint --changed-since <base>`, `bft.py red-on-base`, affected scenario tests, branch coverage on changed files | under 10 minutes |
| Merge to main | Every tier except external contracts; full coverage gate | under 30 minutes |
| Nightly | Everything, including contract tests against sandboxes; optional mutation testing on core domain code | as long as it needs |

A ready-to-edit GitHub Actions workflow is in `assets/github-actions.yml`.

## Silent skips

A test that skips itself when something is missing (a running server, sandbox credentials, a data file) passes
silently. The suite goes green while the behavior goes unchecked, which is why the linter flags skips (BFT007).

When a skip is genuinely useful on a developer laptop, make CI fail instead:

```python
# tests/support/env.py
import os

import pytest


def require(condition, why: str) -> None:
    """Skip locally when an environment is missing; fail in CI, where every tier must really run."""
    if condition:
        return
    if os.environ.get("CI"):
        pytest.fail(f"{why}: CI must run this test", pytrace=False)
    pytest.skip(why)  # bft: allow BFT007 -- local convenience only; CI turns this into a failure
```

When you run tests and a tier could not run, say so in the report. Never count it as passing.

Never let a test default to a live system. A UI test that falls back to `http://localhost:8080` when no URL is
set will, on the machine that runs production, log in to production during an ordinary `pytest`. It can also
run while pytest is only collecting tests, if the module checks the server at import time. Require the target to
be set explicitly for that tier, and treat an unset target as "not configured", not as a default.

## Flaky tests

A flaky test is a bug in the test or in the code. Retrying until green hides it, and so does adding a sleep.
Common causes, roughly in order of frequency:

1. **Time:** reading the real clock, time zones, month ends. Inject a clock.
2. **Shared data:** tests that change rows other tests read, or assert on global counts. Isolate the data.
3. **Order dependence:** a test that only passes after another one has run. Run the suite in random order
   (`pytest-randomly`, `vitest --sequence.shuffle`) to find these.
4. **Unawaited work:** a background task still running when the assertion happens. Wait for the observable result.
5. **Fixed waits:** replace them with condition waits (`real-dependencies.md`).
6. **Real concurrency bugs:** sometimes the flake is the product's race condition. Write the concurrent scenario.

If a flaky test must be quarantined while it is fixed, skip it with an allowance that names the issue, the owner
and a date: `# bft: allow BFT007 -- flaky, issue #123, owner @dana, fix by 2026-10-15`.
