"""Behavior of bft.py, driven the way a developer or CI runs it: a real git repository and the real CLI.

Run the CLI under another interpreter with BFT_PYTHON=/path/to/python (e.g. a Python 3.9 without tomllib).
"""
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

BFT = Path(__file__).resolve().parents[1] / "skills" / "behavior-first-testing" / "scripts" / "bft.py"
PYTHON = os.environ.get("BFT_PYTHON", sys.executable)
PYTEST_RUNNER = (f"{sys.executable} -m pytest {{files}} -q -p no:cacheprovider --continue-on-collection-errors "
                 f"--junitxml={{junit}}")


class Repo:
    def __init__(self, root: Path):
        self.root = root

    def write(self, rel: str, text: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
        return path

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-c", "user.name=bft", "-c", "user.email=bft@example.invalid", *args],
                              cwd=self.root, check=True, capture_output=True, text=True).stdout

    def commit(self, message: str = "change") -> None:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def bft(self, *args: str, stdin: str = None, env: dict = None) -> subprocess.CompletedProcess:
        return subprocess.run([PYTHON, str(BFT), *args], cwd=self.root, input=stdin, capture_output=True,
                              text=True, env={**os.environ, **(env or {})})


@pytest.fixture
def repo(tmp_path):
    r = Repo(tmp_path / "project")
    r.root.mkdir()
    r.git("init", "-q", "-b", "main")
    return r


def rules_reported(result) -> list:
    return [line.split(": ", 1)[1].split(" ", 1)[0] for line in result.stdout.splitlines() if ": BFT" in line]


# --------------------------------------------------------------------------- lint

def test_user_is_told_when_a_test_mocks_a_dependency(repo):
    """
    Given tests that mock a module, patch an attribute, and use pytest-mock
    When the user runs the lint
    Then each is reported as BFT001 and the lint fails
    """
    repo.write("tests/test_orders.py", """
        from unittest.mock import MagicMock

        def test_x(monkeypatch, mocker):
            monkeypatch.setattr("app.gateway.charge", MagicMock())
    """)

    result = repo.bft("lint")

    assert result.returncode == 1
    assert rules_reported(result) == ["BFT001", "BFT001", "BFT001"]
    assert "patches `'app.gateway.charge'`" in result.stdout


def test_user_is_told_when_a_test_stubs_http_with_an_httpx_transport(repo):
    """
    Given a test that answers HTTP calls with httpx's MockTransport, imported both ways
    When the user runs the lint
    Then both are reported as BFT001: canned HTTP answers are an unverified fake
    """
    repo.write("tests/test_sms.py", """
        import httpx
        from httpx import MockTransport

        def test_x():
            one = httpx.MockTransport(lambda request: httpx.Response(201))
            two = MockTransport(lambda request: httpx.Response(400))
    """)

    result = repo.bft("lint")

    assert rules_reported(result) == ["BFT001", "BFT001"]
    assert "stubs HTTP with `httpx.MockTransport`" in result.stdout


def test_user_can_assert_on_what_a_fake_recorded(repo):
    """
    Given a fake payment gateway that records charges, and a test that asserts one charge was recorded
    When the user runs the lint
    Then nothing is reported: an outcome at the system boundary is behavior, not a call count
    """
    repo.write("tests/fakes/gateway.py", """
        class FakeGateway:
            def __init__(self):
                self.charges = []

            def charge(self, order_id, amount):
                self.charges.append((order_id, amount))
    """)
    repo.write("tests/test_checkout.py", """
        def test_user_is_charged_once_when_submitting_twice(shop):
            shop.submit("order-1")
            shop.submit("order-1")
            assert len(shop.gateway.charges) == 1
    """)

    result = repo.bft("lint")

    assert result.returncode == 0, result.stdout
    assert "2 test file(s) checked, no problems" in result.stderr


def test_user_is_told_when_a_test_sleeps_or_reads_the_real_clock(repo):
    """
    Given a test that sleeps, awaits a real delay, yields with sleep(0), and reads today's date
    When the user runs the lint
    Then the sleeps are BFT002, the clock read is BFT003, and sleep(0) is left alone
    """
    repo.write("tests/test_expiry.py", """
        import asyncio
        import time
        from datetime import date

        async def test_x():
            time.sleep(2)
            await asyncio.sleep(1)
            await asyncio.sleep(0)
            assert date.today()
    """)

    result = repo.bft("lint")

    assert rules_reported(result) == ["BFT002", "BFT002", "BFT003"]


def test_user_is_told_when_a_test_asserts_how_code_was_called(repo):
    """
    Given a Python test and a TypeScript test that assert call counts
    When the user runs the lint
    Then both are reported as BFT004, next to the TypeScript mock itself (BFT001)
    """
    repo.write("tests/test_notify.py", """
        def test_x(sender):
            sender.send.assert_called_once()
            assert sender.send.call_count == 1
    """)
    repo.write("web/src/cart.test.ts", """
        import { test, expect, vi } from "vitest";
        test("x", () => {
          const save = vi.fn();
          expect(save).toHaveBeenCalledTimes(1);
        });
    """)

    result = repo.bft("lint")

    assert sorted(rules_reported(result)) == ["BFT001", "BFT004", "BFT004", "BFT004"]


def test_user_sees_scenarios_that_lack_an_actor_or_given_when_then(repo):
    """
    Given acceptance scenarios in Python, TypeScript and Gherkin, some well formed and some not
    When the user runs the lint
    Then only the malformed ones are reported, as BFT005 (actor) and BFT006 (Given / When / Then)
    """
    repo.write("tests/acceptance/test_checkout.py", '''
        def test_user_is_charged_once_when_submitting_the_same_order_twice(shop):
            """
            Given a cart with one item
            When the user submits the order twice
            Then exactly one charge is recorded
            """

        def test_checkout_works(shop):
            # When the order is submitted
            # Then it is accepted
            pass
    ''')
    repo.write("e2e/checkout.spec.ts", """
        test("user sees the order confirmation", async ({ page }) => {
          await test.step("Given a signed-in user with a full cart", async () => {});
          // When they pay
          // Then the confirmation page shows the order number
        });
        test("confirmation email", async () => {
          // Then an email is sent
        });
    """)
    repo.write("specs/refunds.feature", """
        Feature: Refunds
          Background:
            Given a delivered order
          Scenario: user gets a refund for a returned item
            When the user returns the item
            Then the refund is recorded
          Scenario: refund is recorded
            Then the refund is recorded
    """)

    result = repo.bft("lint")
    out = result.stdout

    assert "test_user_is_charged_once" not in out
    assert "user sees the order confirmation" not in out
    assert "user gets a refund" not in out
    assert "test_checkout_works` should start with an actor" in out
    assert "test_checkout_works`: missing Given" in out
    assert "`confirmation email`: missing Given, When" in out
    assert "`refund is recorded`: missing When" in out
    assert sorted(rules_reported(result)) == ["BFT005", "BFT005", "BFT005", "BFT006", "BFT006", "BFT006"]


def test_user_can_allow_a_rule_only_by_giving_a_reason(repo):
    """
    Given one allowance with a reason and one without
    When the user runs the lint
    Then the reasoned allowance silences its line, the bare one is BFT000 and silences nothing
    """
    bare = "# bft" + ": allow BFT002"  # split so this file's own lint does not read it
    repo.write("tests/test_bank.py", f"""
        import time

        def test_x():
            # bft: allow BFT002 -- the bank sandbox rate-limits to one call per second
            time.sleep(1)
            time.sleep(1)  {bare}
    """)

    result = repo.bft("lint")

    assert rules_reported(result) == ["BFT000", "BFT002"]
    assert "test_bank.py:6: BFT000" in result.stdout
    assert "test_bank.py:6: BFT002" in result.stdout


def test_user_is_told_about_skipped_and_focused_tests(repo):
    """
    Given a skipped Python test, a skipped TypeScript test and a focused one
    When the user runs the lint
    Then all three are reported as BFT007
    """
    repo.write("tests/test_report.py", """
        import pytest

        @pytest.mark.skipif(True, reason="flaky")
        def test_x():
            pass
    """)
    repo.write("web/report.test.ts", """
        it.skip("x", () => {});
        test.only("y", () => {});
    """)

    result = repo.bft("lint")

    assert rules_reported(result) == ["BFT007", "BFT007", "BFT007"]


def test_user_is_told_about_coverage_exclusions_without_a_reason(repo):
    """
    Given source files excluding code from coverage, with and without a reason
    When the user runs the lint
    Then only the exclusions without a reason are reported as BFT008
    """
    no_cover, ignore = "# pragma" + ": no cover", "ignore" + " next"  # split so this file's own lint does not read them
    repo.write("app/money.py", f"""
        def refund(order):
            if order is None:  {no_cover}
                raise ValueError
            if order.legacy:  {no_cover} -- legacy orders were migrated in 2024 and cannot occur
                return 0
    """)
    repo.write("web/src/money.ts", f"""
        // Only reachable when the browser lacks Intl, which our support matrix excludes
        /* istanbul {ignore} */
        export const fmt = (n: number) => n.toFixed(2);
        // todo
        /* c8 {ignore} */
        export const x = 1;
    """)

    result = repo.bft("lint")

    assert rules_reported(result) == ["BFT008", "BFT008"]
    assert "app/money.py:2: BFT008" in result.stdout
    assert "web/src/money.ts:5: BFT008" in result.stdout


def test_user_can_lint_only_what_their_branch_changed(repo):
    """
    Given a legacy test with a violation on main, and a branch adding a test with its own violation
    When the user lints with --changed-since main
    Then only the branch's violation is reported
    """
    repo.write("tests/test_legacy.py", "import time\n\ndef test_old():\n    time.sleep(1)\n")
    repo.commit("legacy")
    repo.git("checkout", "-q", "-b", "feature")
    repo.write("tests/test_new.py", "from unittest import mock\n")

    result = repo.bft("lint", "--changed-since", "main")

    assert rules_reported(result) == ["BFT001"]
    assert "test_legacy" not in result.stdout


def test_user_configures_actors_and_scenario_folders(repo):
    """
    Given a config that adds `admin` as an actor and puts scenarios under specs/
    When the user runs the lint
    Then admin scenarios pass, other actors fail, and tests outside specs/ are not treated as scenarios
    """
    repo.write(".behavior-testing.toml", """
        # comments, a multi-line array and a literal string must all parse without tomllib
        actors = [
            "user",
            'admin',   # back office
        ]
        scenario_globs = ["specs/**"]
    """)
    body = '    """\n    Given x\n    When y\n    Then z\n    """\n'
    repo.write("specs/test_refunds.py", f"def test_admin_approves_a_refund():\n{body}\n"
                                        f"def test_system_retries_a_refund():\n{body}")
    repo.write("tests/acceptance/test_other.py", "def test_anything():\n    pass\n")

    result = repo.bft("lint")

    assert rules_reported(result) == ["BFT005"]
    assert "test_system_retries_a_refund" in result.stdout
    assert "should start with an actor (user / admin)" in result.stdout


def test_user_gets_annotations_in_github_actions(repo):
    """
    Given a test with a violation
    When CI runs the lint with --format github
    Then the violation is printed as a GitHub error annotation
    """
    repo.write("tests/test_x.py", "import time\ntime.sleep(3)\n")

    result = repo.bft("lint", "--format", "github")

    assert result.stdout.startswith("::error file=tests/test_x.py,line=2,title=BFT002::sleeps")


# --------------------------------------------------------------------------- scenarios

def test_user_can_list_scenarios_for_a_product_manager(repo):
    """
    Given scenarios written in Python and TypeScript
    When the user lists scenarios
    Then each appears with a readable title and its Given / When / Then lines
    """
    repo.write("tests/acceptance/test_checkout.py", '''
        def test_user_is_charged_once_when_submitting_the_same_order_twice(shop):
            """
            Given a cart with one item
            When the user submits the order twice
            And the network retries the second request
            Then exactly one charge is recorded
            """
    ''')
    repo.write("e2e/login.spec.ts", """
        test("user is locked out after five wrong passwords", async () => {
          // Given an account
          // When the wrong password is entered five times
          // Then the account is locked
        });
    """)

    out = repo.bft("scenarios").stdout

    assert "- **user is charged once when submitting the same order twice**" in out
    assert "  - And the network retries the second request" in out
    assert "  - Then the account is locked" in out
    assert "_2 scenario(s)._" in out


# --------------------------------------------------------------------------- guard

SCENARIO = '''
    def test_user_is_charged_once_when_submitting_the_same_order_twice(shop):
        """
        Given a cart with one item
        When the user submits the order twice
        Then exactly one charge is recorded
        """
        assert len(shop.gateway.charges) == 1
'''


def edit_event(repo, rel, old, new):
    return json.dumps({"tool_name": "Edit", "cwd": str(repo.root),
                       "tool_input": {"file_path": str(repo.root / rel), "old_string": old, "new_string": new}})


def test_user_blocks_claude_from_rewriting_an_approved_scenario(repo):
    """
    Given a committed scenario and a project that has opted in with a config file
    When Claude tries to change the scenario's assertion
    Then the hook blocks the edit with exit code 2 and explains what to do instead
    """
    repo.write(".behavior-testing.toml", "actors = ['user']\n")
    repo.write("tests/acceptance/test_checkout.py", SCENARIO)
    repo.commit("approved scenario")

    result = repo.bft("guard", stdin=edit_event(repo, "tests/acceptance/test_checkout.py", "== 1", "== 2"))

    assert result.returncode == 2
    assert "changes approved scenario(s)" in result.stderr
    assert "stop and ask the human" in result.stderr


def test_user_still_lets_claude_add_scenarios_and_edit_drafts(repo):
    """
    Given a committed scenario file and an uncommitted draft scenario file
    When Claude appends a new scenario to the committed file, and edits the draft
    Then neither edit is blocked
    """
    repo.write(".behavior-testing.toml", "actors = ['user']\n")
    repo.write("tests/acceptance/test_checkout.py", SCENARIO)
    repo.commit("approved scenario")
    repo.write("tests/acceptance/test_refunds.py", SCENARIO)
    anchor = "        assert len(shop.gateway.charges) == 1\n"
    addition = anchor + "\n\ndef test_user_can_cancel_before_payment(shop):\n    pass\n"

    append = repo.bft("guard", stdin=edit_event(repo, "tests/acceptance/test_checkout.py", anchor, addition))
    draft = repo.bft("guard", stdin=edit_event(repo, "tests/acceptance/test_refunds.py", "== 1", "== 2"))

    assert (append.returncode, draft.returncode) == (0, 0), append.stderr + draft.stderr


def test_user_can_unlock_scenarios_or_leave_the_guard_off(repo):
    """
    Given a committed scenario
    When Claude edits it with BFT_UNLOCK_SCENARIOS=1 set, or in a project with no config file
    Then the edit is allowed
    """
    repo.write("tests/acceptance/test_checkout.py", SCENARIO)
    repo.commit("approved scenario")
    event = edit_event(repo, "tests/acceptance/test_checkout.py", "== 1", "== 2")

    no_config = repo.bft("guard", stdin=event)
    repo.write(".behavior-testing.toml", "actors = ['user']\n")
    unlocked = repo.bft("guard", stdin=event, env={"BFT_UNLOCK_SCENARIOS": "1"})

    assert (no_config.returncode, unlocked.returncode) == (0, 0)


# --------------------------------------------------------------------------- red-on-base

def pricing_repo(repo):
    repo.write("pyproject.toml", "[tool.pytest.ini_options]\npythonpath = [\".\"]\n")
    repo.write("app.py", "def price(amount, member):\n    return amount\n")
    repo.write("tests/test_price.py", """
        from app import price

        def test_user_pays_the_list_price():
            assert price(100, member=False) == 100
    """)
    repo.commit("list price")
    repo.git("checkout", "-q", "-b", "feature")
    repo.write("app.py", "def price(amount, member):\n    return amount * 9 // 10 if member else amount\n")


NEW_TESTS = """

def test_user_who_is_a_member_gets_ten_percent_off():
    assert price(100, member=True) == 90


{allow}def test_user_who_is_not_a_member_pays_the_list_price():
    assert price(50, member=False) == 50
"""


def test_user_is_told_when_a_new_test_already_passes_on_the_base_branch(repo):
    """
    Given a branch that adds a member discount, with one test for the discount and one for old behavior
    When the user runs red-on-base
    Then the discount test is red on base (good) and the other is flagged as BFT010
    """
    pricing_repo(repo)
    with open(repo.root / "tests/test_price.py", "a") as f:
        f.write(NEW_TESTS.format(allow=""))
    repo.commit("member discount")

    result = repo.bft("red-on-base", "--base", "main", "--command", PYTEST_RUNNER)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "red      tests/test_price.py::test_user_who_is_a_member_gets_ten_percent_off" in result.stdout
    assert "PASSES   tests/test_price.py::test_user_who_is_not_a_member_pays_the_list_price" in result.stdout
    assert "test_user_pays_the_list_price" not in result.stdout


def test_user_sees_new_tests_in_a_file_that_cannot_load_on_base_as_red(repo):
    """
    Given a branch that adds a module, a new test file importing it, and new tests in an existing file
    When the user runs red-on-base with pytest told to continue past collection errors
    Then the new file's tests count as red because it cannot load on base, and the other file still runs
    """
    pricing_repo(repo)
    repo.write("rounding.py", "def to_nickel(cents):\n    return (cents + 2) // 5 * 5\n")
    repo.write("tests/test_rounding.py", """
        from rounding import to_nickel

        def test_user_paying_cash_is_rounded_to_the_nickel():
            assert to_nickel(1002) == 1000

        def test_user_paying_cash_is_rounded_up_from_three_cents():
            assert to_nickel(1003) == 1005
    """)

    with open(repo.root / "tests/test_price.py", "a") as f:
        f.write(NEW_TESTS.format(allow="# bft: allow BFT010 -- guards list price for non-members\n"))

    result = repo.bft("red-on-base", "--base", "main", "--command", PYTEST_RUNNER)

    assert result.returncode == 0, result.stdout + result.stderr
    assert ("red      tests/test_rounding.py::test_user_paying_cash_is_rounded_to_the_nickel  "
            "(the file fails to load on base)") in result.stdout
    assert "red      tests/test_price.py::test_user_who_is_a_member_gets_ten_percent_off" in result.stdout
    assert "NOT RUN" not in result.stdout


def test_user_sees_new_tests_whose_shared_setup_cannot_load_on_base_as_red(repo):
    """
    Given a branch that adds a module, a conftest fixture importing it, and a new test using that fixture
    When the user runs red-on-base
    Then pytest cannot load the setup on the base branch, which counts as red, not as "not run"
    """
    pricing_repo(repo)
    repo.write("clock.py", "def today():\n    return '2026-09-19'\n")
    repo.write("tests/conftest.py", """
        import pytest
        from clock import today

        @pytest.fixture
        def shop_today():
            return today()
    """)
    with open(repo.root / "tests/test_price.py", "a") as f:
        f.write("\n\ndef test_user_sees_prices_as_of_the_shop_day(shop_today):\n    assert shop_today == '2026-09-19'\n")

    result = repo.bft("red-on-base", "--base", "main", "--command", PYTEST_RUNNER)

    assert result.returncode == 0, result.stdout + result.stderr
    assert ("red      tests/test_price.py::test_user_sees_prices_as_of_the_shop_day  "
            "(the test setup fails to load on base)") in result.stdout


def test_user_sees_a_marked_guard_as_allowed_once_the_new_setup_can_load(repo):
    """
    Given a new conftest that imports a module the branch adds, and a marked guard that uses its fixture
    When the user runs red-on-base with --with-new-files
    Then the guard passes once the new files are present and is reported as allowed, with its reason
    """
    pricing_repo(repo)
    repo.write("clock.py", "def today():\n    return '2026-09-19'\n")
    repo.write("tests/conftest.py", """
        import pytest
        from clock import today

        @pytest.fixture
        def shop_today():
            return today()
    """)
    with open(repo.root / "tests/test_price.py", "a") as f:
        f.write("\n\n# bft: allow BFT010 -- guards list price, which the new clock must not change\n"
                "def test_user_pays_list_price_on_the_shop_day(shop_today):\n"
                "    assert price(100, member=False) == 100\n")

    result = repo.bft("red-on-base", "--base", "main", "--command", PYTEST_RUNNER, "--with-new-files")

    assert result.returncode == 0, result.stdout + result.stderr
    assert ("allowed  tests/test_price.py::test_user_pays_list_price_on_the_shop_day  (passes once the new files "
            "are present: guards list price, which the new clock must not change)") in result.stdout


def test_user_can_lend_an_ignored_file_to_the_base_checkout(repo):
    """
    Given tests that need an ignored .env file, which a fresh checkout of the base branch lacks
    When the user runs red-on-base without --link, and then with --link .env
    Then the first cannot run the tests and the second can
    """
    pricing_repo(repo)
    repo.write(".gitignore", ".env\n")
    repo.write(".env", "SHOP=vancouver\n")
    with open(repo.root / "tests/test_price.py", "a") as f:
        f.write(NEW_TESTS.format(allow="# bft: allow BFT010 -- guards list price for non-members\n"))
    command = f"test -f .env && {PYTEST_RUNNER}"

    without = repo.bft("red-on-base", "--base", "main", "--command", command)
    lent = repo.bft("red-on-base", "--base", "main", "--command", command, "--link", ".env")

    assert "NOT RUN" in without.stdout
    assert lent.returncode == 0, lent.stdout + lent.stderr
    assert "red      tests/test_price.py::test_user_who_is_a_member_gets_ten_percent_off" in lent.stdout
    assert (repo.root / ".env").read_text() == "SHOP=vancouver\n"


def test_user_can_tell_tests_that_pin_changed_behavior_from_tests_of_new_code_alone(repo):
    """
    Given a branch that changes pricing and adds a rounding module, with a new test for each
    When the user runs red-on-base with --with-new-files
    Then the pricing test fails on its own assertion even with the new module present (strong evidence),
      and the rounding test fails only while the module is missing (it tests the new code on its own)
    """
    pricing_repo(repo)
    repo.write("rounding.py", "def to_nickel(cents):\n    return (cents + 2) // 5 * 5\n")
    repo.write("tests/test_rounding.py", """
        from rounding import to_nickel

        def test_user_paying_cash_is_rounded_to_the_nickel():
            assert to_nickel(1002) == 1000
    """)
    with open(repo.root / "tests/test_price.py", "a") as f:
        f.write(NEW_TESTS.format(allow="# bft: allow BFT010 -- guards list price for non-members\n"))

    plain = repo.bft("red-on-base", "--base", "main", "--command", PYTEST_RUNNER)
    both = repo.bft("red-on-base", "--base", "main", "--command", PYTEST_RUNNER, "--with-new-files")

    assert plain.returncode == both.returncode == 0, both.stdout + both.stderr
    assert "(fails on its own assertion)" not in plain.stdout
    assert ("red      tests/test_price.py::test_user_who_is_a_member_gets_ten_percent_off  "
            "(fails on its own assertion)") in both.stdout
    assert ("red      tests/test_rounding.py::test_user_paying_cash_is_rounded_to_the_nickel  "
            "(fails only while the new files are missing: it tests new code on its own)") in both.stdout
    assert "allowed  tests/test_price.py::test_user_who_is_not_a_member_pays_the_list_price" in both.stdout


def test_user_can_mark_a_characterization_test_that_is_meant_to_pass_on_base(repo):
    """
    Given the same branch, where the old-behavior test is marked as pinning existing behavior
    When the user runs red-on-base
    Then it is reported as allowed, with its reason, and the check passes
    """
    pricing_repo(repo)
    allow = "# bft: allow BFT010 -- pins list price for non-members before the refactor\n"
    with open(repo.root / "tests/test_price.py", "a") as f:
        f.write(NEW_TESTS.format(allow=allow))

    result = repo.bft("red-on-base", "--base", "main", "--command", PYTEST_RUNNER)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "allowed  tests/test_price.py::test_user_who_is_not_a_member_pays_the_list_price" in result.stdout
    assert "(passes on base: pins list price for non-members before the refactor)" in result.stdout


def test_user_is_told_when_new_tests_could_not_be_run_on_base(repo):
    """
    Given a runner command that produces no JUnit report
    When the user runs red-on-base
    Then the new tests are NOT RUN, the runner's problem is shown, and the check fails unless allowed
    """
    pricing_repo(repo)
    with open(repo.root / "tests/test_price.py", "a") as f:
        f.write(NEW_TESTS.format(allow=""))

    strict = repo.bft("red-on-base", "--base", "main", "--command", "echo no tests here")
    lenient = repo.bft("red-on-base", "--base", "main", "--command", "echo no tests here", "--allow-not-run")

    assert strict.returncode == 1
    assert "NOT RUN" in strict.stdout
    assert "wrote no JUnit XML" in strict.stderr and "no tests here" in strict.stderr
    assert lenient.returncode == 0


def test_user_red_on_base_leaves_the_repository_as_it_was(repo):
    """
    Given a branch with new tests
    When the user runs red-on-base
    Then no worktree is left behind and the working tree is unchanged
    """
    pricing_repo(repo)
    with open(repo.root / "tests/test_price.py", "a") as f:
        f.write(NEW_TESTS.format(allow=""))
    before = repo.git("status", "--porcelain")

    repo.bft("red-on-base", "--base", "main", "--command", PYTEST_RUNNER)

    assert repo.git("worktree", "list").count("\n") == 1
    assert repo.git("status", "--porcelain") == before


def test_user_keeps_their_virtualenv_which_red_on_base_borrows(repo):
    """
    Given an ignored .venv in the repository
    When the user runs red-on-base with a runner that needs that .venv
    Then the runner finds it inside the base checkout, and afterwards the real .venv is untouched
    """
    pricing_repo(repo)
    repo.write(".gitignore", ".venv/\n")
    marker = repo.write(".venv/marker", "installed packages live here\n")
    with open(repo.root / "tests/test_price.py", "a") as f:
        f.write(NEW_TESTS.format(allow=""))

    result = repo.bft("red-on-base", "--base", "main", "--command", f"test -f .venv/marker && {PYTEST_RUNNER}")

    assert "red      tests/test_price.py::test_user_who_is_a_member_gets_ten_percent_off" in result.stdout
    assert marker.read_text() == "installed packages live here\n"


# --------------------------------------------------------------------------- gaps

COBERTURA = """<?xml version="1.0" ?>
<coverage line-rate="0.8" branch-rate="0.75" branches-valid="4" branches-covered="3">
  <sources><source>{src}</source></sources>
  <packages><package name="app"><classes>
    <class name="refunds.py" filename="app/refunds.py">
      <lines>
        <line number="1" hits="1"/>
        <line number="2" hits="1" branch="true" condition-coverage="50% (1/2)"/>
        <line number="3" hits="0"/>
        <line number="4" hits="1"/>
        <line number="5" hits="0"/>
        <line number="6" hits="0"/>
        <line number="8" hits="0"/>
        <line number="11" hits="1"/>
        <line number="12" hits="0"/>
      </lines>
    </class>
    <class name="errors.py" filename="app/errors.py">
      <lines><line number="1" hits="0"/><line number="2" hits="0"/></lines>
    </class>
  </classes></package></packages>
</coverage>
"""


def test_user_gets_a_gap_table_with_failure_paths_first(repo):
    """
    Given a coverage report with an untested error branch, untested happy-path lines, an untested import,
      and a file no test ever loaded
    When the user asks for the gaps
    Then the failure path is listed first, then the rest by file and line number, with consecutive lines
      grouped, the unloaded file as one row, the import not mistaken for a failure path, and every row
      waiting for a reason
    """
    repo.write("app/refunds.py", """
        def refund(order):
            if order.paid_by_card:
                raise RefundNotAllowed("card refunds go through the processor")
            total = order.total
            order.refunded = total
            return total

        from errors import RefundNotAllowed


        def audit(order):
            return order.id
    """)
    repo.write("coverage.xml", COBERTURA.format(src=repo.root))

    result = repo.bft("gaps", "coverage.xml")
    rows = [line.split(" | ")[1:4] for line in result.stdout.splitlines() if line.startswith("| ") and "`app/" in line]

    assert "Branch coverage **75.0%**" in result.stdout
    assert rows == [
        ["`app/refunds.py:3`", "never ran", "yes"],
        ["`app/errors.py`", "whole file never ran (2 lines)", ""],
        ["`app/refunds.py:2`", "branch 1/2 taken", ""],
        ["`app/refunds.py:5-6`", "never ran", ""],
        ["`app/refunds.py:8`", "never ran", ""],
        ["`app/refunds.py:12`", "never ran", ""],
    ]
    assert all(line.endswith("| |") for line in result.stdout.splitlines() if line.startswith("| ") and "`app/" in line)


def test_user_fails_the_build_when_branch_coverage_is_under_the_bar(repo):
    """
    Given a coverage report at 75% branch coverage
    When the user asks for the gaps with --fail-under 90, and with --fail-under 70
    Then the first fails and the second passes
    """
    repo.write("app/refunds.py", "x = 1\n" * 6)
    repo.write("coverage.xml", COBERTURA.format(src=repo.root))

    under = repo.bft("gaps", "coverage.xml", "--fail-under", "90")
    over = repo.bft("gaps", "coverage.xml", "--fail-under", "70")

    assert (under.returncode, over.returncode) == (1, 0)
    assert "branch coverage 75.0% is under 90%" in under.stderr


# --------------------------------------------------------------------------- init

def test_user_gets_a_starter_config_that_knows_their_test_runners(repo):
    """
    Given a repository with a pytest backend and a Vitest frontend
    When the user runs init, and then runs it again
    Then a config is written with a runner for each and the branch that exists as its base, it parses,
      and a second init refuses to overwrite it
    """
    repo.write("backend/pyproject.toml", "[tool.pytest.ini_options]\ntestpaths = ['tests']\n")
    repo.write("frontend/package.json", '{"devDependencies": {"vitest": "^3.0.0"}}\n')
    repo.commit("first commit")

    first = repo.bft("init")
    second = repo.bft("init")
    config = (repo.root / ".behavior-testing.toml").read_text()
    lint = repo.bft("lint")

    assert first.returncode == 0 and second.returncode == 1
    assert 'base = "main"' in config
    assert "[red_on_base.runners.backend]" in config and 'cwd = "backend"' in config
    assert "npx vitest run {files} --reporter=junit --outputFile={junit}" in config
    assert lint.returncode == 0, lint.stderr
