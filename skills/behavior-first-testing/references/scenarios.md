# Writing acceptance scenarios

A scenario is one behavior the business cares about, stated so that a user or PM can read it and say "yes,
that's what should happen". The test code then makes it executable.

## Contents
- What makes a good scenario
- Naming and Given / When / Then in each framework
- Finding the scenarios that matter (checklist)
- Approval and protection
- Bug fixes
- Reviewing scenarios with non-engineers

## What makes a good scenario

- **One behavior.** Split any title that needs an "and".
- **Domain language.** Write "a customer with an unpaid invoice", not "a row in `invoices` with `status=2`".
  Endpoints and table names belong in the test code, not in the Given/When/Then text.
- **Observable outcome.** The Then says what someone could see: a response, a page, a balance, an email, a charge.
- **Concrete.** "the total is $107.10" beats "the total is correct". Exact values make the rule visible.
- **Independent.** Each scenario creates the state it needs. It never relies on another scenario having run first.

## Naming

Names start with the actor (default `user`; others come from `actors` in `.behavior-testing.toml`) and read as a
sentence: who, what, under which condition.

| Good | Why the alternative is worse |
|---|---|
| `user_is_charged_once_when_submitting_the_same_order_twice` | `test_idempotency`: which behavior? |
| `user_cannot_refund_more_than_was_paid` | `test_refund_validation`: validating what? |
| `admin_sees_overdue_invoices_first` | `test_sort_order`: implementation vocabulary |
| `system_cancels_unpaid_orders_after_48_hours` | `test_cron_job`: says how, not what |

## Given / When / Then per framework

### pytest (preferred form: a docstring)

The docstring is what `bft.py scenarios` prints for reviewers. `# Given` comments inside the body are optional
signposts.

```python
def test_user_is_charged_once_when_submitting_the_same_order_twice(client, payments, customer):
    """
    Given a customer with one item in the cart
    When they submit the order twice with the same idempotency key
    Then exactly one charge is recorded
    And both responses return the same order number
    """
    order = {"customer_id": customer.id, "lines": [{"sku": "GLASS-6MM", "qty": 1}]}
    headers = {"Idempotency-Key": "checkout-1"}

    first = client.post("/api/orders", json=order, headers=headers)
    second = client.post("/api/orders", json=order, headers=headers)

    assert first.status_code == second.status_code == 201
    assert first.json()["number"] == second.json()["number"]
    assert len(payments.charges_for(customer.id)) == 1
```

`payments` is a fake payment gateway injected into the app. It records charges the way the real processor would.
See `real-dependencies.md`.

### Vitest / Jest

```ts
test("user sees an error and is not charged when the card is declined", async () => {
  // Given a customer whose card the processor will decline
  payments.declineNextCharge("insufficient_funds");
  // When they submit the order
  const res = await api.post("/api/orders").send(order);
  // Then the response explains the decline and no order or charge exists
  expect(res.status).toBe(402);
  expect(res.body.error).toBe("card_declined");
  expect(await api.get(`/api/orders?customer=${customer.id}`)).toMatchObject({ body: [] });
  expect(payments.charges).toHaveLength(0);
});
```

### Playwright (steps also appear in the HTML report)

```ts
test("user sees the order confirmation with the order number", async ({ page }) => {
  await test.step("Given a signed-in customer with a full cart", async () => {
    await signIn(page, customer);
    await addToCart(page, "6mm tempered glass", 2);
  });
  await test.step("When they pay by card", async () => {
    await page.getByRole("button", { name: "Pay" }).click();
  });
  await test.step("Then the confirmation shows the order number", async () => {
    await expect(page.getByRole("heading", { name: /Order #\d+/ })).toBeVisible();
  });
});
```

### Gherkin (`.feature`, with pytest-bdd, Cucumber or Playwright-BDD)

Use feature files only when non-engineers edit scenarios themselves. Otherwise the step-definition layer is
maintenance without a reader.

```gherkin
Feature: Refunds
  Background:
    Given a delivered order paid by card for $120.00

  Scenario: user gets a partial refund for one returned item
    When the user returns one item worth $40.00
    Then a refund of $40.00 is recorded
    And the order shows $80.00 paid
```

## Finding the scenarios that matter

Start from the happy path, then walk this list for the rule you are changing. Most production incidents in
business systems live in the second half.

- **Rule boundaries.** For every threshold, test just below, at, and just above it. Examples: minimum order,
  free-shipping limit, credit limit.
- **Invalid input.** Missing, malformed, negative or out-of-range values. Check what the user is told.
- **Not found and not allowed.** Another tenant's record, a deleted record, a role without permission.
- **Conflicts and duplicates.** Double submit, retry after a timeout, two users editing the same record.
- **Concurrency.** Two requests at once on the same balance or inventory. Send them concurrently; sequential calls
  don't test this.
- **External failure.** Decline, timeout, 5xx, a malformed response, or a success that arrives late. What state is
  left behind?
- **Partial failure.** Step 2 of 3 fails: is anything half-written? Is the user told the truth?
- **State transitions.** Every illegal transition is refused: refund before payment, ship after cancel.
- **Time.** End of month, leap day, daylight-saving change, time zones, expiry at exactly the deadline.
- **Money.** Rounding (half-up vs banker's), tax on discounted amounts, currency, totals across many lines.
- **Idempotency of jobs.** A scheduled job that runs twice, or restarts halfway.

You won't need all of them for every change. Say which ones you considered and skipped.

## Approval and protection

1. Present the scenarios as a list of titles, each with its Given/When/Then, and nothing else. Reviewers should not
   need to read code.
2. The human approves, edits, or adds to the list. Approval is theirs to give. When they say "just proceed", note
   that in the report.
3. Write the tests from the approved text. If implementation reveals the text was ambiguous, ask. Don't
   reinterpret it silently.
4. Committing the scenario file marks it approved. With the guard hook installed (`enforcement.md`), Claude Code
   cannot change a committed scenario; it can still add new ones. On pull requests, CODEOWNERS on the scenario
   folders gives the same protection for humans.

## Bug fixes

Reproduce the bug as a scenario first, from the user's side ("user is not charged twice when the payment
page is refreshed"). Watch it fail for the reason in the bug report, then fix the code. `bft.py red-on-base`
checks the same thing mechanically: the new scenario must fail on the base branch.

## Reviewing scenarios with non-engineers

```bash
python3 <skill-dir>/scripts/bft.py scenarios > scenarios.md
```

This prints every scenario grouped by file, with its Given/When/Then lines. Share it with a PM before
implementation, or attach the diff of this file to a pull request so reviewers see behavior changes at a glance.
