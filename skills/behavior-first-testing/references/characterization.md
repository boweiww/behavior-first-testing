# Characterization tests: pinning a legacy system's behavior

When you replace or rewrite a system that has been running the business (an old ERP module, a spreadsheet, a
monolith's billing code), its past outputs are the most complete specification you will get. A
characterization test runs historical inputs through the new code and compares the results with what the old
system produced, field by field.

## When to use it

- You have real historical inputs and outputs: past orders with their computed totals, invoices, payroll runs,
  quotes.
- The rules are too many or too implicit to write down as scenarios before you start.
- You are refactoring code whose behavior must not change.

This works alongside scenarios. Scenarios state the rules people agreed on; the characterization test catches
the rules nobody remembered.

## The pattern

1. **Compare exactly.** Every computed field of every historical record is compared exactly: money to the cent,
   quantities and dates exactly. Never with a tolerance. A tolerance hides precisely the rounding and ordering
   rules this test exists to find.
2. **Pin the known differences, each with a reason.** Some differences are real: the old system had a bug, a
   record was edited by hand, a rule changed on purpose. Put each one in a pinned file with its identity, the
   expected and actual values, and a reason someone verified by hand.
3. **Fail in both directions.** The test fails when a new difference appears, which means a rule broke. It also
   fails when a pinned difference disappears, which means something changed and the pin must be re-verified.
4. **Reclassify when a rule changes on purpose.** Regenerate the difference list and review every new entry by
   hand before committing it. Don't bulk-accept.
5. **Keep an exact subset.** Some suites must match with no pins at all, for example hand-checked records or
   records covering known bug classes. Any difference there is a failure.

```python
# tests/characterization/test_pricing_matches_history.py
"""Every computed field of every historical order, priced by the new engine, compared exactly.

If this fails, either a rule broke (fix the code) or you changed a rule on purpose: regenerate with
`python -m tests.characterization.harness --dump`, verify every new entry by hand, then commit.
Never widen a tolerance to make it pass.
"""
import json
from pathlib import Path

from .harness import run_history  # -> list of Diff(record, field, expected, got)

PINNED = Path(__file__).with_name("pinned_diffs.json")
KEY = ("record", "field", "expected", "got")


# bft: allow BFT010 -- characterization test: it pins existing behavior, so it passes on the base branch by design
def test_system_prices_every_historical_order_as_the_old_system_did():
    pinned = {tuple(d[k] for k in KEY): d["reason"] for d in json.loads(PINNED.read_text())}
    got = {tuple(str(getattr(d, k)) for k in KEY) for d in run_history()}

    new = sorted(got - set(pinned))
    vanished = sorted(set(pinned) - got)

    assert not new, f"{len(new)} new difference(s), a rule changed or broke:\n" + "\n".join(map(str, new))
    assert not vanished, (f"{len(vanished)} pinned difference(s) vanished, re-verify them:\n"
                          + "\n".join(f"{k} ({pinned[k]})" for k in vanished))
```

```json
[
  {"record": "SO-10442", "field": "fees.gst.base", "expected": "1204.37", "got": "1204.38",
   "reason": "old system summed unrounded line totals; new engine rounds per line, as the tax rules require"}
]
```

## Data

- Historical data often contains customer details. Keep it out of the repository: load it from a path in an
  environment variable, or from an anonymized extract.
- When the data is absent, the test must not pass silently. Use the `require()` pattern from
  `e2e-and-tiers.md` so it skips on a laptop without the data but fails in CI.
- Record where the data came from and when it was exported, next to the pinned file.

## Retiring it

Once the new system has run in production long enough and scenarios cover the rules that matter, the
characterization suite can shrink to its exact subset or be retired. Decide that explicitly; don't let it rot
into a skipped test.
