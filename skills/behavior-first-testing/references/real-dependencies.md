# Real dependencies, verified fakes, clocks

## Contents
- Vocabulary: mock, stub, fake
- PostgreSQL: isolation strategies
- Redis and other infrastructure
- External systems: fakes with contract tests
- Failure injection
- Time
- Waiting without sleeping
- Randomness and IDs

## Vocabulary

| | What it is | Allowed? |
|---|---|---|
| **Real dependency** | The actual PostgreSQL, Redis, queue or filesystem, run locally or in a container | Yes: the default for infrastructure you run |
| **Fake** | A working, simplified implementation of *your own* interface to an external system (an in-memory payment gateway that records charges and enforces idempotency) | Yes, with a contract test against the real system |
| **Mock / spy** | An object programmed with expected calls, or a recorder of calls you then assert on | No (BFT001, BFT004) |
| **Patch** | Replacing a module attribute at runtime (`monkeypatch.setattr`, `mock.patch`, `vi.mock`) | No (BFT001). Inject the dependency instead |
| **HTTP stub** | Canned responses at the HTTP layer (`responses`, `respx`, `nock`, `msw`, `page.route`) | Only as a fake's implementation detail, with a contract test and a reasoned allowance |

Substitutes that behave differently from production, such as SQLite standing in for PostgreSQL, are not "real". SQL
semantics, locking, constraints and types all differ. Use the same engine and major version as production.

## PostgreSQL: isolation strategies

Build the schema with the **same migrations production runs** (`alembic upgrade head`, `prisma migrate deploy`),
not `metadata.create_all()`. Then the tests also catch broken migrations.

Pick an isolation strategy by how the test reaches the database:

| Strategy | Use when | Notes |
|---|---|---|
| **Rollback per test**: each test runs in a transaction that is rolled back | In-process API tests where the app uses the session you give it | Fastest. Doesn't work when the code under test opens its own connections or threads |
| **Unique data per test**: each test creates its own customer, tenant or order, with unique keys | A separately running server (UI end-to-end), background workers, or anything multi-connection | Never assert on global counts ("there are 3 orders"); assert on your own records |
| **Database per worker**: `CREATE DATABASE app_test_gw0 TEMPLATE app_test_template` | Parallel runs (pytest-xdist, Vitest threads) | Cloning a template database is fast; migrate the template once |

Rollback per test with SQLAlchemy 2.x and FastAPI:

```python
# tests/conftest.py
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import get_session
from app.main import app

engine = create_engine(os.environ["TEST_DATABASE_URL"])


@pytest.fixture
def db():
    """Each test runs inside one outer transaction that is rolled back. Commits made by the app
    become savepoints inside it, so the app behaves as in production."""
    with engine.connect() as conn:
        outer = conn.begin()
        session = Session(bind=conn, join_transaction_mode="create_savepoint")
        try:
            yield session
        finally:
            session.close()
            outer.rollback()


@pytest.fixture
def client(db):
    app.dependency_overrides[get_session] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()
```

Shared session-wide data (seed products, tax rates) is fine as long as it is read-only. If a test needs to change
a shared setting, give that test its own copy of the data or its own tenant. When tests mutate shared rows, they
depend on their order, which becomes flaky once they run in parallel.

In CI, run PostgreSQL as a service container, or use Testcontainers:

```python
from testcontainers.postgres import PostgresContainer

with PostgresContainer("postgres:17") as pg:
    url = pg.get_connection_url()
```

```ts
import { PostgreSqlContainer } from "@testcontainers/postgresql";
const pg = await new PostgreSqlContainer("postgres:17").start();
const url = pg.getConnectionUri();
```

## Redis and other infrastructure

- **Redis:** use a real server. Give each test a key prefix, or use a dedicated database index that is flushed
  between tests (`FLUSHDB` on db 15, never on the one the app uses in development).
- **Queues and brokers** (RabbitMQ, Kafka, SQS through LocalStack): run the real broker. Assert on what a
  consumer would receive, or on the state the consumer produces.
- **Object storage:** use a real temporary directory, or an S3-compatible server (MinIO) for S3 code paths.
- **Email and SMS:** these are external systems. Use a fake outbox behind your own sender interface, or a local
  capture server (Mailpit) for SMTP.

## External systems: fakes with contract tests

The pattern has three parts:

1. **A port.** Your code depends on your own small interface, not on the vendor SDK.
2. **A fake.** It implements the port in memory and behaves the way the vendor does, including idempotency,
   validation and error types. It also has controls for scenarios, such as `decline_next_charge()`.
3. **A contract test.** It runs the same assertions against the fake and against the vendor's sandbox. When the
   vendor changes behavior, the sandbox run fails and you update the fake. Without it, the fake slowly becomes a
   mock with extra steps.

```python
# app/payments.py: the port
class PaymentGateway(Protocol):
    def charge(self, customer_id: str, amount: Decimal, idempotency_key: str) -> Charge: ...
    def refund(self, charge_id: str, amount: Decimal) -> Refund: ...


# tests/fakes/payments.py: the fake
class FakePaymentGateway:
    def __init__(self):
        self.charges: list[Charge] = []
        self._by_key: dict[str, Charge] = {}
        self._decline: str | None = None

    def charge(self, customer_id, amount, idempotency_key):
        if idempotency_key in self._by_key:
            return self._by_key[idempotency_key]
        if self._decline:
            reason, self._decline = self._decline, None
            raise CardDeclined(reason)
        charge = Charge(id=f"ch_{len(self.charges) + 1}", customer_id=customer_id, amount=amount)
        self.charges.append(charge)
        self._by_key[idempotency_key] = charge
        return charge

    def decline_next_charge(self, reason: str) -> None:
        self._decline = reason

    def charges_for(self, customer_id: str) -> list[Charge]:
        return [c for c in self.charges if c.customer_id == customer_id]


# tests/contract/test_payment_gateway.py: one suite, two implementations
@pytest.fixture(params=["fake", "sandbox"])
def gateway(request):
    if request.param == "fake":
        return FakePaymentGateway()
    key = os.environ.get("PAYMENTS_SANDBOX_KEY")
    require(key, "PAYMENTS_SANDBOX_KEY is not set")  # skips locally, fails in CI: see e2e-and-tiers.md
    return StripeGateway(api_key=key)


def test_a_repeated_idempotency_key_returns_the_same_charge(gateway):
    first = gateway.charge("cus_1", Decimal("10.00"), idempotency_key="k-1")
    second = gateway.charge("cus_1", Decimal("10.00"), idempotency_key="k-1")
    assert first.id == second.id


def test_a_declined_card_raises_card_declined(gateway, declining_card):
    with pytest.raises(CardDeclined):
        gateway.charge(declining_card, Decimal("10.00"), idempotency_key="k-2")
```

Wire the fake into the app by dependency injection, not by patching. For example, use a FastAPI
`app.dependency_overrides[get_payment_gateway] = lambda: fake`, a constructor argument, or a DI container binding.
Run the sandbox half of the contract suite nightly, and on any change to the adapter.

## Failure injection

Failure paths need tests too, and you can rarely get a real dependency to fail on demand:

- **External systems:** give the fake controls: `decline_next_charge()`, `time_out_next_call()`,
  `respond_slowly(seconds)`. Cover the same failures in the contract test using the vendor's documented test
  cards or accounts.
- **Network to your own infrastructure:** put Toxiproxy between the app and PostgreSQL or Redis. Add latency, cut
  the connection, and check what the user is told and what state is left behind.
- **PostgreSQL specifics:** create the real conditions.
  - Unique violations: insert the duplicate.
  - Serialization failures: run two concurrent transactions.
  - Lock timeouts: have a second connection hold `SELECT ... FOR UPDATE` while the app has `lock_timeout` set.

## Time

Inject a clock and control it from the test. Reading the real clock in a test (BFT003) makes it depend on when it
runs. Tests that compute "today" pass all day and fail just after midnight, at the month end, or in another time
zone.

```python
# app/clock.py
class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

def get_clock() -> SystemClock:        # a FastAPI dependency; services take a clock argument
    return SystemClock()


# tests/support/clock.py
class FakeClock:
    def __init__(self, start: datetime):
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, **delta) -> None:
        self._now += timedelta(**delta)


# tests/conftest.py
@pytest.fixture
def clock():
    c = FakeClock(datetime(2026, 3, 31, 23, 59, tzinfo=ZoneInfo("America/Vancouver")))
    app.dependency_overrides[get_clock] = lambda: c
    yield c
    app.dependency_overrides.pop(get_clock, None)
```

```python
def test_user_sees_an_invoice_as_overdue_the_day_after_it_is_due(client, clock, customer):
    """
    Given an invoice on net-30 terms issued on March 31
    When April 30 ends
    Then the invoice is listed as overdue
    """
    invoice = issue_invoice(client, customer, terms_days=30)
    clock.advance(days=30, minutes=2)
    assert client.get(f"/api/invoices/{invoice['id']}").json()["status"] == "overdue"
```

Other tools:

- **Legacy code that calls `datetime.now()` directly:** `time-machine` or `freezegun` are fake clocks. They are
  allowed, but inject a clock when you touch that code.
- **Vitest and Jest:** `vi.useFakeTimers(); vi.setSystemTime(new Date("2026-03-31T23:59:00-07:00"))`.
- **Playwright:** `await page.clock.install({ time: new Date("2026-03-31T23:59:00-07:00") })`, then
  `page.clock.runFor(...)` or `page.clock.fastForward(...)`.
- **A separately running server** (UI end-to-end): the server reads time from an injectable clock. A test-only
  endpoint sets it, and is enabled by an environment variable that production startup refuses.
- **Database time:** `DEFAULT now()` and SQL `now()` ignore your fake clock. For business-meaningful timestamps
  (due dates, expiry, cut-offs), write the value from the application's clock.

## Waiting without sleeping

A fixed sleep (BFT002) is too long on a fast machine and too short on a slow CI runner. Wait for the condition:

- **Playwright:** web-first assertions (`await expect(locator).toBeVisible()`) retry automatically. For anything
  else, use `expect.poll(() => fetchStatus()).toBe("paid")`.
- **Vitest:** use `await vi.waitFor(() => expect(outbox.messages).toHaveLength(1))`.
- **Python:** use a small helper whose only sleep is the polling interval:

```python
def wait_until(condition, timeout=5.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while True:
        result = condition()
        if result:
            return result
        if time.monotonic() > deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        time.sleep(interval)  # bft: allow BFT002 -- polling interval; the wait ends on the condition, not the timer
```

If the delay is time-based business logic (retry after 5 minutes, expire after 48 hours), advance the fake clock
instead of waiting.

## Randomness and IDs

- Seed every random source a test depends on (`random.seed`, Faker's seed, property-based testing seeds are
  printed on failure).
- When an ID or token appears in an outcome you assert on, inject its generator. Otherwise, assert on the shape
  (`matches r"^ord_[0-9a-z]{12}$"`) and use the returned value in the next step.
