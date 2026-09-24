#!/usr/bin/env python3
"""bft: the checks behind the behavior-first-testing skill.

    bft.py lint [PATH...] [--changed-since REF] [--format text|github]
    bft.py scenarios [PATH...]          list scenarios as Given / When / Then, for people to review
    bft.py red-on-base [--base REF]     new tests must fail on the base branch
    bft.py gaps COVERAGE_XML [--changed-since REF] [--fail-under PCT]
    bft.py guard                        Claude Code PreToolUse hook (reads the event on stdin)
    bft.py init [--force]               write a starter .behavior-testing.toml
    bft.py rules                        list the rule ids

Standard library only, Python 3.9+. Configuration lives in .behavior-testing.toml at the
repository root; every key has a default, so the file is optional for everything except `guard`.
"""
from __future__ import annotations

import argparse
import ast
import copy
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

CONFIG_NAME = ".behavior-testing.toml"

RULES = {
    "BFT000": "Allowance without a reason, or naming an unknown rule.",
    "BFT001": "Mocking. Use the real dependency, or a fake whose contract is verified against the real system.",
    "BFT002": "Sleeping. Wait for an observable condition, or advance a fake clock.",
    "BFT003": "Reading the real clock. Inject a clock and control it from the test.",
    "BFT004": "Asserting how code was called. Assert the outcome a user or downstream system can observe.",
    "BFT005": "Scenario name does not start with an actor (see `actors` in the config).",
    "BFT006": "Scenario does not state Given, When and Then, in that order.",
    "BFT007": "Skipped or focused test. A skip is a silent pass; a focus hides the rest of the suite.",
    "BFT008": "Coverage exclusion without a reason.",
    "BFT010": "New test already passes on the base branch, so it does not pin the new behavior.",
}

PY_EXTS = {".py"}
JS_EXTS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts"}
FEATURE_EXTS = {".feature"}

DEFAULTS: dict = {
    "test_globs": [
        "**/test_*.py", "**/*_test.py", "**/conftest.py", "**/tests/**", "**/test/**",
        "**/__tests__/**", "**/e2e/**", "**/*.test.*", "**/*.spec.*", "**/*.feature",
    ],
    "scenario_globs": ["**/tests/acceptance/**", "**/tests/e2e/**", "**/e2e/**", "**/*.feature"],
    "source_globs": ["**/*.py", "**/*.ts", "**/*.tsx", "**/*.js", "**/*.jsx"],
    "exclude_globs": [
        "**/node_modules/**", "**/.venv/**", "**/venv/**", "**/site-packages/**", "**/dist/**",
        "**/build/**", "**/coverage/**", "**/__pycache__/**", "**/.git/**",
    ],
    "actors": ["user"],
    "guard": {"enabled": True},
    "red_on_base": {"base": "origin/main", "copy_globs": [], "link": [], "runners": {}},
}

SUPPORT_GLOBS = ["**/tests/**", "**/test/**", "**/__tests__/**", "**/e2e/**", "**/fixtures/**", "**/conftest.py"]


# --------------------------------------------------------------------------- config

def _parse_toml(text: str) -> dict:
    try:
        import tomllib  # Python 3.11+
        return tomllib.loads(text)
    except ModuleNotFoundError:
        pass
    try:
        import tomli  # type: ignore
        return tomli.loads(text)
    except ModuleNotFoundError:
        return _mini_toml(text)


def _strip_comment(line: str) -> str:
    out, quote = [], None
    for ch in line:
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            break
        out.append(ch)
    return "".join(out)


def _toml_value(v: str):
    out, i = [], 0
    while i < len(v):
        ch = v[i]
        if ch == '"':
            j = i + 1
            while j < len(v) and (v[j] != '"' or v[j - 1] == "\\"):
                j += 1
            out.append(v[i:j + 1])
            i = j + 1
        elif ch == "'":
            j = v.index("'", i + 1)
            out.append(json.dumps(v[i + 1:j]))
            i = j + 1
        else:
            out.append(ch)
            i += 1
    return json.loads(re.sub(r",\s*]", "]", "".join(out)))


def _mini_toml(text: str) -> dict:
    """The TOML this config uses, for Pythons without tomllib: [tables], strings, bools, numbers, arrays."""
    data: dict = {}
    table = data
    pending = ""
    for raw in text.splitlines():
        line = _strip_comment(raw).strip()
        if pending:
            pending += " " + line
            if pending.count("[") > pending.count("]"):
                continue
            line, pending = pending, ""
        if not line:
            continue
        if line.startswith("[") and "=" not in line:
            table = data
            for part in line.strip("[] ").split("."):
                table = table.setdefault(part.strip().strip('"'), {})
            continue
        key, _, value = line.partition("=")
        if value.count("[") > value.count("]"):
            pending = line
            continue
        table[key.strip().strip('"')] = _toml_value(value.strip())
    return data


def load_config(root: Path) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    path = root / CONFIG_NAME
    if path.exists():
        for key, value in _parse_toml(path.read_text(encoding="utf-8")).items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(value)
            else:
                cfg[key] = value
    return cfg


# --------------------------------------------------------------------------- git and files

def git(root: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise SystemExit(f"bft: git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def git_show(root: Path, rev: str, rel: str) -> Optional[str]:
    r = subprocess.run(["git", "show", f"{rev}:{rel}"], cwd=root, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def repo_root(start: Path) -> Path:
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=start, capture_output=True, text=True)
    return Path(r.stdout.strip()) if r.returncode == 0 else start.resolve()


def list_files(root: Path) -> List[str]:
    r = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=root,
                       capture_output=True, text=True)
    if r.returncode == 0:
        files = [f for f in r.stdout.split("\0") if f]
    else:
        files = []
        skip = {".git", "node_modules", ".venv", "venv", "__pycache__"}
        for d, dirs, names in os.walk(root):
            dirs[:] = [x for x in dirs if x not in skip]
            files += [Path(d, n).relative_to(root).as_posix() for n in names]
    return sorted(f for f in files if (root / f).is_file())


def changed_files(root: Path, ref: str) -> Set[str]:
    """Files that differ from the merge-base with REF: committed, staged, unstaged and untracked."""
    base = git(root, "merge-base", ref, "HEAD").strip()
    changed = git(root, "diff", "--name-only", "--diff-filter=ACMR", base).splitlines()
    untracked = git(root, "ls-files", "-o", "--exclude-standard").splitlines()
    return {f for f in changed + untracked if f}


def read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


_GLOB_CACHE: Dict[str, "re.Pattern[str]"] = {}


def _glob_re(pattern: str) -> "re.Pattern[str]":
    if pattern not in _GLOB_CACHE:
        i, out = 0, []
        while i < len(pattern):
            if pattern.startswith("**/", i):
                out.append("(?:.*/)?")
                i += 3
            elif pattern.startswith("**", i):
                out.append(".*")
                i += 2
            elif pattern[i] == "*":
                out.append("[^/]*")
                i += 1
            elif pattern[i] == "?":
                out.append("[^/]")
                i += 1
            else:
                out.append(re.escape(pattern[i]))
                i += 1
        _GLOB_CACHE[pattern] = re.compile("".join(out) + r"\Z")
    return _GLOB_CACHE[pattern]


def matches(rel: str, patterns) -> bool:
    rel = rel.replace(os.sep, "/")
    return any(_glob_re(p).match(rel) for p in patterns)


def kind_of(rel: str) -> Optional[str]:
    ext = Path(rel).suffix
    if ext in PY_EXTS:
        return "py"
    if ext in JS_EXTS:
        return "js"
    if ext in FEATURE_EXTS:
        return "feature"
    return None


def is_test_file(rel: str, cfg: dict) -> bool:
    return (kind_of(rel) is not None and matches(rel, cfg["test_globs"])
            and not matches(rel, cfg["exclude_globs"]))


def is_scenario_file(rel: str, cfg: dict) -> bool:
    return is_test_file(rel, cfg) and matches(rel, cfg["scenario_globs"])


def is_source_file(rel: str, cfg: dict) -> bool:
    return (kind_of(rel) in ("py", "js") and matches(rel, cfg["source_globs"])
            and not matches(rel, cfg["exclude_globs"]))


def scope_filter(paths: List[str], root: Path):
    if not paths:
        return lambda rel: True
    prefixes = [os.path.relpath(Path(p).resolve(), root.resolve()).replace(os.sep, "/") for p in paths]
    prefixes = ["" if p == "." else p for p in prefixes]
    return lambda rel: any(p == "" or rel == p or rel.startswith(p.rstrip("/") + "/") for p in prefixes)


# --------------------------------------------------------------------------- allowances

SUPPRESS_RE = re.compile(r"bft:\s*allow(?P<file>-file)?\s+(?P<ids>BFT\d{3}(?:\s*,\s*BFT\d{3})*)(?P<rest>.*)", re.I)
COMMENT_ONLY_RE = re.compile(r"^\s*(#|//|/\*|\*)")


@dataclass
class Violation:
    path: str
    line: int
    rule: str
    message: str
    span: Optional[Tuple[int, int]] = None


@dataclass
class Allowances:
    lines: Dict[int, Dict[str, str]] = field(default_factory=dict)
    file: Dict[str, str] = field(default_factory=dict)
    problems: List[Violation] = field(default_factory=list)

    def reason(self, rule: str, first: int, last: int) -> Optional[str]:
        if rule in self.file:
            return self.file[rule]
        for n in range(first, last + 1):
            if rule in self.lines.get(n, {}):
                return self.lines[n][rule]
        return None

    def allows(self, v: Violation) -> bool:
        first, last = v.span or (v.line, v.line)
        return self.reason(v.rule, first, last) is not None


def read_allowances(rel: str, lines: List[str]) -> Allowances:
    """`bft: allow BFT001 -- why` covers its own line, or the next code line when it stands alone.
    `bft: allow-file BFT001 -- why` covers the whole file. A reason is required."""
    a = Allowances()
    for i, text in enumerate(lines, 1):
        m = SUPPRESS_RE.search(text)
        if not m:
            continue
        ids = {x.strip().upper() for x in m.group("ids").split(",")}
        reason = re.sub(r"^[\s:\u2014\u2013-]+", "", m.group("rest"))
        reason = re.sub(r"(\*/|\"\"\"|''')\s*$", "", reason).strip()
        unknown = ids - set(RULES)
        if unknown:
            a.problems.append(Violation(rel, i, "BFT000", f"unknown rule id(s): {', '.join(sorted(unknown))}"))
        if len(reason) < 10:
            a.problems.append(Violation(rel, i, "BFT000",
                                        "an allowance needs a reason a reviewer can judge: `bft: allow BFTnnn -- <why>`"))
            continue
        target = a.file if m.group("file") else a.lines.setdefault(i, {})
        for rid in ids:
            target[rid] = reason
        if not m.group("file") and COMMENT_ONLY_RE.match(text):
            j = i  # index of the line after this one
            while j < len(lines) and (not lines[j].strip() or COMMENT_ONLY_RE.match(lines[j])):
                j += 1
            for rid in ids:
                a.lines.setdefault(j + 1, {})[rid] = reason
    return a


# --------------------------------------------------------------------------- test discovery

STEP_RE = re.compile(r"^\s*(?:#|//|/\*+|\*)?\s*(given|when|then|and|but)\b[\s:,-]*(.*?)\s*(?:\*/)?$", re.I)
JS_STEP_CALL_RE = re.compile(r"""\bstep\s*\(\s*(['"`])\s*(given|when|then|and|but)\b[\s:,-]*(.*?)\1""", re.I)
JS_TEST_RE = re.compile(
    r"""^\s*(?:it|test)(?:\.(?:concurrent|sequential|each\s*\(.*?\)))?\s*\(\s*(['"`])(?P<title>(?:\\.|(?!\1).)*)\1""")


@dataclass
class TestCase:
    name: str      # stable id within the file: "test_x", "TestClass::test_x", or the JS/feature title
    title: str     # what a person reads
    line: int      # the def / test( / Scenario: line
    start: int     # first line, including decorators or a leading comment block
    end: int
    steps: List[Tuple[str, str]] = field(default_factory=list)


def _step(text: str) -> Optional[Tuple[str, str]]:
    m = STEP_RE.match(text)
    return (m.group(1).lower(), m.group(2)) if m else None


def py_tests(tree: ast.AST, lines: List[str]) -> List[TestCase]:
    out: List[TestCase] = []

    def visit(body, prefix: str) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                start = min([d.lineno for d in node.decorator_list] + [node.lineno])
                end = getattr(node, "end_lineno", None) or node.lineno
                steps = [s for s in map(_step, (ast.get_docstring(node) or "").splitlines()) if s]
                for text in lines[node.lineno - 1:end]:
                    if text.strip().startswith("#"):
                        s = _step(text)
                        if s:
                            steps.append(s)
                title = node.name[4:].lstrip("_").replace("_", " ")
                out.append(TestCase(prefix + node.name, title, node.lineno, start, end, steps))
            elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                visit(node.body, prefix + node.name + "::")

    visit(getattr(tree, "body", []), "")
    return out


def js_tests(lines: List[str]) -> List[TestCase]:
    starts = []
    for i, text in enumerate(lines, 1):
        m = JS_TEST_RE.match(text)
        if m:
            starts.append((i, m.group("title")))
    out = []
    for idx, (ln, title) in enumerate(starts):
        end = starts[idx + 1][0] - 1 if idx + 1 < len(starts) else len(lines)
        start = ln
        while start > 1 and COMMENT_ONLY_RE.match(lines[start - 2]):
            start -= 1
        steps = []
        for n in range(start, end + 1):
            text = lines[n - 1]
            if n != ln and COMMENT_ONLY_RE.match(text):
                s = _step(text)
                if s:
                    steps.append(s)
            for m in JS_STEP_CALL_RE.finditer(text):
                steps.append((m.group(2).lower(), m.group(3)))
        out.append(TestCase(title, title, ln, start, end, steps))
    return out


def feature_tests(lines: List[str]) -> List[TestCase]:
    tests: List[TestCase] = []
    current: Optional[TestCase] = None
    in_background = background_given = False
    for i, text in enumerate(lines, 1):
        s = text.strip()
        m = re.match(r"(?:Scenario(?: Outline| Template)?|Example):\s*(.*)", s)
        if m:
            current = TestCase(m.group(1), m.group(1), i, i, i)
            tests.append(current)
            in_background = False
            continue
        if re.match(r"Background:", s):
            in_background, current = True, None
            continue
        if re.match(r"(?:Feature|Rule):", s):
            in_background, current = False, None
            continue
        m = re.match(r"(Given|When|Then|And|But|\*)\s+(.*)", s)
        if m:
            if in_background:
                background_given = True
            elif current:
                current.steps.append((m.group(1).lower(), m.group(2)))
                current.end = i
    if background_given:
        for t in tests:
            if not any(k == "given" for k, _ in t.steps):
                t.steps.insert(0, ("given", "(see Background)"))
    return tests


def tests_in(rel: str, text: Optional[str]) -> List[TestCase]:
    if text is None:
        return []
    kind = kind_of(rel)
    lines = text.splitlines()
    if kind == "py":
        try:
            return py_tests(ast.parse(text), lines)
        except SyntaxError:
            return []
    if kind == "js":
        return js_tests(lines)
    if kind == "feature":
        return feature_tests(lines)
    return []


def gwt_problem(steps: List[Tuple[str, str]]) -> Optional[str]:
    kws = [k for k, _ in steps if k in ("given", "when", "then")]
    missing = [k.capitalize() for k in ("given", "when", "then") if k not in kws]
    if missing:
        return "missing " + ", ".join(missing)
    if not kws.index("given") < kws.index("when") < kws.index("then"):
        return "Given / When / Then are out of order"
    return None


def _norm_actor(actor: str) -> str:
    return re.sub(r"[\s-]+", "_", actor.strip().lower())


def actor_ok(t: TestCase, kind: str, actors: List[str]) -> bool:
    if kind == "py":
        base = t.name.split("::")[-1][4:].lstrip("_").lower()
        return any(base == _norm_actor(a) or base.startswith(_norm_actor(a) + "_") for a in actors)
    title = t.title.strip().lower()
    for a in actors:
        a = a.strip().lower()
        if title == a or (title.startswith(a) and not title[len(a)].isalnum() and title[len(a)] != "_"):
            return True
    return False


# --------------------------------------------------------------------------- lint rules

MOCK_MODULES = ("unittest.mock", "mock", "pytest_mock", "asynctest", "flexmock", "doublex")
HTTP_STUB_MODULES = ("responses", "respx", "requests_mock", "httpretty", "aioresponses", "pook")
HTTP_STUB_CALLS = {"httpx.MockTransport"}
SLEEPS = {"time.sleep"}
ASYNC_SLEEPS = {"asyncio.sleep", "trio.sleep", "anyio.sleep", "gevent.sleep"}
CLOCKS = {
    "datetime.datetime.now", "datetime.datetime.utcnow", "datetime.datetime.today", "datetime.date.today",
    "time.time", "time.time_ns", "django.utils.timezone.now", "pendulum.now", "pendulum.today",
    "arrow.now", "arrow.utcnow",
}
SKIPS = {
    "pytest.mark.skip", "pytest.mark.skipif", "pytest.mark.xfail", "pytest.skip", "pytest.xfail",
    "pytest.importorskip", "unittest.skip", "unittest.skipIf", "unittest.skipUnless", "unittest.expectedFailure",
}
INTERACTION_ATTRS = {
    "assert_called", "assert_called_once", "assert_called_with", "assert_called_once_with", "assert_any_call",
    "assert_has_calls", "assert_not_called", "assert_awaited", "assert_awaited_once", "assert_awaited_with",
    "assert_awaited_once_with", "assert_any_await", "assert_has_awaits", "assert_not_awaited",
    "call_count", "call_args", "call_args_list", "mock_calls", "method_calls", "await_count", "await_args",
}

MSG = {
    "mock": "mocks `{}`: use the real dependency (database, cache, queue), or a fake of the external system "
            "whose contract test runs against the real one",
    "http": "stubs HTTP with `{}`: put a fake behind your own client interface and verify it with a contract test",
    "mocker": "uses the `mocker` fixture (pytest-mock): use the real dependency or a contract-verified fake",
    "patch": "patches `{}`: inject the dependency instead (a constructor argument, a FastAPI dependency override)",
    "sleep": "sleeps (`{}`): wait for an observable condition, or advance a fake clock",
    "clock": "reads the real clock (`{}`): inject a clock and set it from the test",
    "interaction": "asserts on calls (`{}`): assert what a user or downstream system can observe instead",
    "skip": "skips (`{}`): a skipped test passes silently; fix it, delete it, or allow it with a reason",
    "focus": "focuses a test (`{}`): the rest of the suite stops running",
}


def _in_modules(name: str, modules) -> bool:
    return any(name == m or name.startswith(m + ".") for m in modules)


def _dotted(node) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


class _Resolver:
    def __init__(self, tree: ast.AST):
        self.aliases: Dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    self.aliases[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
            elif isinstance(node, ast.ImportFrom) and node.module:
                for a in node.names:
                    self.aliases[a.asname or a.name] = f"{node.module}.{a.name}"

    def __call__(self, node) -> str:
        d = _dotted(node)
        if not d:
            return ""
        head, _, rest = d.partition(".")
        full = self.aliases.get(head, head)
        return f"{full}.{rest}" if rest else full


def check_py(rel: str, text: str, lines: List[str], scenario: bool, actors: List[str]) -> List[Violation]:
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        print(f"bft: {rel}:{e.lineno}: cannot parse ({e.msg}); skipped", file=sys.stderr)
        return []
    resolve = _Resolver(tree)
    out: List[Violation] = []

    def add(line: int, rule: str, key: str, what: str = "") -> None:
        out.append(Violation(rel, line, rule, MSG[key].format(what)))

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            else:
                names = [node.module or ""] + [f"{node.module}.{a.name}" for a in node.names]
            for n in names:
                if _in_modules(n, MOCK_MODULES):
                    add(node.lineno, "BFT001", "mock", n)
                    break
                if _in_modules(n, HTTP_STUB_MODULES):
                    add(node.lineno, "BFT001", "http", n)
                    break
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(a.arg == "mocker" for a in node.args.args + node.args.kwonlyargs):
                add(node.lineno, "BFT001", "mocker")
        elif isinstance(node, ast.Call):
            q = resolve(node.func)
            first_is_zero = bool(node.args) and isinstance(node.args[0], ast.Constant) and node.args[0].value == 0
            if q in SLEEPS or q.endswith("wait_for_timeout") or (q in ASYNC_SLEEPS and not first_is_zero):
                add(node.lineno, "BFT002", "sleep", q)
            elif q in CLOCKS:
                add(node.lineno, "BFT003", "clock", q)
            elif q in HTTP_STUB_CALLS:
                add(node.lineno, "BFT001", "http", q)
            elif q.endswith("monkeypatch.setattr") or q.endswith("monkeypatch.delattr"):
                target = ast.unparse(node.args[0]) if node.args and hasattr(ast, "unparse") else "an attribute"
                add(node.lineno, "BFT001", "patch", target)
            elif q in SKIPS or q.endswith(".skipTest"):
                add(node.lineno, "BFT007", "skip", q)
        elif isinstance(node, ast.Attribute):
            if node.attr in INTERACTION_ATTRS:
                add(node.lineno, "BFT004", "interaction", node.attr)
            elif resolve(node) in SKIPS:
                add(node.lineno, "BFT007", "skip", resolve(node))

    if scenario:
        for t in py_tests(tree, lines):
            out += scenario_violations(rel, t, "py", actors)
    return out


JS_MOCK_CALL = re.compile(r"\b(?:vi|jest)\.(?:mock|doMock|unmock|spyOn|fn|stubGlobal|mocked|createMockFromModule)\s*\("
                          r"|\b(?:page|context|browserContext)\.route(?:FromHAR)?\s*\(")
JS_MOCK_IMPORT = re.compile(r"""(?:\bfrom\s+|\brequire\s*\(\s*|\bimport\s*\(\s*|^\s*import\s+)['"]"""
                            r"""(sinon|testdouble|nock|msw|jest-mock-extended|vitest-mock-extended|ts-mockito"""
                            r"""|jest-fetch-mock|fetch-mock|axios-mock-adapter)(?:/[^'"]*)?['"]""")
JS_SLEEP = re.compile(r"\bsetTimeout\s*\(|\bwaitForTimeout\s*\(|\b(?:sleep|delay|wait)\s*\(\s*\d")
JS_CLOCK = re.compile(r"\bDate\.now\s*\(|\bnew\s+Date\s*\(\s*\)|\bperformance\.now\s*\(")
JS_INTERACTION = re.compile(r"\.(?:toHaveBeenCalled\w*|toBeCalled\w*|toHaveBeenLastCalledWith|toHaveBeenNthCalledWith"
                            r"|lastCalledWith|nthCalledWith|toHaveReturned\w*|toReturn\w*)\s*\(|\.mock\.(?:calls|results|instances)\b")
JS_FOCUS = re.compile(r"\b(?:it|test|describe|suite)\.only\s*\(|\b(?:fit|fdescribe)\s*\(")
JS_SKIP = re.compile(r"\b(?:it|test|describe|suite)\.(?:skip|fixme|todo)\s*\(|\b(?:xit|xtest|xdescribe)\s*\(")


def check_js(rel: str, lines: List[str], scenario: bool, actors: List[str]) -> List[Violation]:
    out: List[Violation] = []
    rules = [
        (JS_MOCK_IMPORT, "BFT001", "mock"), (JS_MOCK_CALL, "BFT001", "mock"), (JS_SLEEP, "BFT002", "sleep"),
        (JS_CLOCK, "BFT003", "clock"), (JS_INTERACTION, "BFT004", "interaction"),
        (JS_FOCUS, "BFT007", "focus"), (JS_SKIP, "BFT007", "skip"),
    ]
    for i, text in enumerate(lines, 1):
        if COMMENT_ONLY_RE.match(text):
            continue
        for rx, rule, key in rules:
            m = rx.search(text)
            if m:
                what = m.group(1) if rx is JS_MOCK_IMPORT else m.group(0).rstrip("( ").lstrip(".")
                out.append(Violation(rel, i, rule, MSG[key].format(what)))
    if scenario:
        for t in js_tests(lines):
            out += scenario_violations(rel, t, "js", actors)
    return out


def scenario_violations(rel: str, t: TestCase, kind: str, actors: List[str]) -> List[Violation]:
    out = []
    span = (max(1, t.start - 1), t.line)
    if not actor_ok(t, kind, actors):
        who = " / ".join(actors)
        example = f"test_{_norm_actor(actors[0])}_..." if kind == "py" else f'"{actors[0]} ..."'
        out.append(Violation(rel, t.line, "BFT005",
                             f"scenario `{t.name}` should start with an actor ({who}), e.g. {example}", span))
    problem = gwt_problem(t.steps)
    if problem:
        where = "in the docstring or as # comments" if kind == "py" else "as // comments or test.step() titles"
        if kind == "feature":
            where = "as steps"
        out.append(Violation(rel, t.line, "BFT006", f"scenario `{t.name}`: {problem} ({where})", span))
    return out


PRAGMA_RE = re.compile(r"(pragma:\s*no\s*(?:cover|branch)|\b(?:istanbul|c8|v8)\s+ignore\b"
                       r"(?:\s+(?:next|if|else|file|start|stop|line))?(?:\s+\d+)?)", re.I)


def check_pragmas(rel: str, lines: List[str]) -> List[Violation]:
    out = []
    for i, text in enumerate(lines, 1):
        m = PRAGMA_RE.search(text)
        if not m or m.group(0).lower().endswith("stop"):
            continue
        reason = re.sub(r"(\*/|@preserve)", "", text[m.end():])
        reason = re.sub(r"^[\s:\u2014\u2013,;-]+", "", reason).strip()
        prev = lines[i - 2] if i > 1 else ""
        prev_text = re.sub(r"^[\s#/*-]+", "", prev).strip()
        prev_ok = bool(COMMENT_ONLY_RE.match(prev)) and not PRAGMA_RE.search(prev) and len(prev_text) >= 10
        if len(reason) < 10 and not prev_ok:
            out.append(Violation(rel, i, "BFT008",
                                 f"`{m.group(0).strip()}` needs a reason: why is this not tested, and why is that acceptable?"))
    return out


def lint_file(rel: str, text: str, cfg: dict) -> List[Violation]:
    lines = text.splitlines()
    found: List[Violation] = []
    if is_test_file(rel, cfg):
        scenario = is_scenario_file(rel, cfg)
        kind = kind_of(rel)
        if kind == "py":
            found += check_py(rel, text, lines, scenario, cfg["actors"])
        elif kind == "js":
            found += check_js(rel, lines, scenario, cfg["actors"])
        elif kind == "feature" and scenario:
            for t in feature_tests(lines):
                found += scenario_violations(rel, t, "feature", cfg["actors"])
    if kind_of(rel) in ("py", "js"):
        found += check_pragmas(rel, lines)
    allow = read_allowances(rel, lines)
    seen, kept = set(), []
    for v in found:
        if (v.line, v.rule, v.message) in seen or allow.allows(v):
            continue
        seen.add((v.line, v.rule, v.message))
        kept.append(v)
    return allow.problems + kept


# --------------------------------------------------------------------------- commands

def cmd_lint(args, root: Path, cfg: dict) -> int:
    in_scope = scope_filter(args.paths, root)
    files = [f for f in list_files(root) if in_scope(f) and (is_test_file(f, cfg) or is_source_file(f, cfg))]
    if args.changed_since:
        changed = changed_files(root, args.changed_since)
        files = [f for f in files if f in changed]
    violations: List[Violation] = []
    tests_checked = 0
    for rel in files:
        text = read_text(root / rel)
        if text is None:
            continue
        tests_checked += is_test_file(rel, cfg)
        violations += lint_file(rel, text, cfg)
    violations.sort(key=lambda v: (v.path, v.line, v.rule))
    for v in violations:
        if args.format == "github":
            msg = v.message.replace("%", "%25").replace("\n", "%0A")
            print(f"::error file={v.path},line={v.line},title={v.rule}::{msg}")
        else:
            print(f"{v.path}:{v.line}: {v.rule} {v.message}")
    if violations:
        n_files = len({v.path for v in violations})
        print(f"\nbft: {len(violations)} problem(s) in {n_files} file(s). "
              f"Fix them, or allow one with `bft: allow BFTnnn -- <reason>`.", file=sys.stderr)
        return 1
    print(f"bft: {tests_checked} test file(s) checked, no problems.", file=sys.stderr)
    return 0


def cmd_rules(args, root: Path, cfg: dict) -> int:
    for rid, text in RULES.items():
        print(f"{rid}  {text}")
    return 0


def cmd_scenarios(args, root: Path, cfg: dict) -> int:
    in_scope = scope_filter(args.paths, root)
    total = 0
    print("# Scenarios\n")
    for rel in list_files(root):
        if not (in_scope(rel) and is_scenario_file(rel, cfg)):
            continue
        tests = tests_in(rel, read_text(root / rel))
        if not tests:
            continue
        print(f"## {rel}\n")
        for t in tests:
            total += 1
            print(f"- **{t.title}**")
            for kw, text in t.steps:
                print(f"  - {kw.capitalize()} {text}".rstrip())
        print()
    print(f"_{total} scenario(s)._")
    return 0


@dataclass
class JCase:
    name: str
    classname: str
    file: str
    status: str


def parse_junit(path: Path) -> List[JCase]:
    try:
        tree = ET.parse(path)
    except (ET.ParseError, OSError):
        return []
    out = []
    for tc in tree.iter("testcase"):
        tags = {child.tag for child in tc}
        status = "failed" if tags & {"failure", "error"} else "skipped" if "skipped" in tags else "passed"
        out.append(JCase(tc.get("name", ""), tc.get("classname", ""), tc.get("file", ""), status))
    return out


def _cases_for(rel: str, t: TestCase, cases: List[JCase]) -> List[JCase]:
    stem = Path(rel).with_suffix("").as_posix()
    hits = []
    for c in cases:
        if kind_of(rel) == "py":
            cls, _, func = t.name.rpartition("::")
            if c.name.split("[")[0] != func:
                continue
            cn = c.classname
            if cls:
                if not (cn == cls or cn.endswith("." + cls)):
                    continue
                cn = cn[: -len(cls)].rstrip(".")
            if cn and not stem.endswith(cn.replace(".", "/")):
                continue
        else:
            if not (c.name == t.title or c.name.endswith(t.title)):
                continue
            where = c.file or c.classname
            if where and Path(rel).name not in where and Path(rel).stem not in where:
                continue
        hits.append(c)
    return hits


def _file_failed_to_load(rel: str, cases: List[JCase]) -> bool:
    """pytest reports a module that cannot be imported as one failed case named after the module."""
    stem = Path(rel).with_suffix("").as_posix()
    for c in cases:
        if c.status != "failed" or not c.name:
            continue
        name = c.name.replace("\\", "/")
        if name.endswith(Path(rel).name) or ("." in name and stem.endswith(name.replace(".", "/"))):
            return True
    return False


def _links(root: Path, rob: dict, extra: Optional[List[str]] = None) -> List[str]:
    names = list(rob.get("link") or []) + list(extra or [])
    for cand in (".venv", "venv", "node_modules"):
        for p in [root / cand, *sorted(root.glob(f"*/{cand}"))]:
            if p.is_dir():
                names.append(p.relative_to(root).as_posix())
    seen, out = set(), []
    for n in names:
        if n not in seen and (root / n).exists():
            seen.add(n)
            out.append(n)
    return out


def _runners(args, rob: dict) -> List[dict]:
    if args.command:
        return [{"name": "cli", "globs": ["**"], "cwd": args.cwd or ".", "command": args.command}]
    runners = [dict(v, name=k) for k, v in (rob.get("runners") or {}).items()]
    if rob.get("command"):
        runners.append({"name": "default", "globs": ["**"], "cwd": rob.get("cwd", "."), "command": rob["command"]})
    return runners


def cmd_red_on_base(args, root: Path, cfg: dict) -> int:
    rob = cfg["red_on_base"]
    base_ref = args.base or rob.get("base") or "origin/main"
    runners = _runners(args, rob)
    if not runners:
        print("bft red-on-base: no runner configured. Add [red_on_base.runners.<name>] with `globs`, `cwd` and a "
              "`command` that runs {files} and writes JUnit XML to {junit} (see `bft.py init`), or pass --command.",
              file=sys.stderr)
        return 2
    merge_base = git(root, "merge-base", base_ref, "HEAD").strip()
    changed = sorted(changed_files(root, base_ref))

    new: List[Tuple[str, TestCase]] = []
    for rel in changed:
        if kind_of(rel) not in ("py", "js") or not is_test_file(rel, cfg) or not (root / rel).exists():
            continue
        before = {t.name for t in tests_in(rel, git_show(root, merge_base, rel))}
        new += [(rel, t) for t in tests_in(rel, read_text(root / rel)) if t.name not in before]
    if not new:
        print(f"bft red-on-base: no new tests since {base_ref} ({merge_base[:9]}).")
        return 0

    by_runner: Dict[int, List[str]] = {}
    unassigned: Set[str] = set()
    for rel in sorted({r for r, _ in new}):
        idx = next((i for i, r in enumerate(runners) if matches(rel, r.get("globs") or ["**"])), None)
        if idx is None:
            unassigned.add(rel)
        else:
            by_runner.setdefault(idx, []).append(rel)

    copy_globs = list(cfg["test_globs"]) + SUPPORT_GLOBS + list(rob.get("copy_globs") or [])
    to_copy = [f for f in changed if (root / f).is_file() and matches(f, copy_globs)]
    tmp = Path(tempfile.mkdtemp(prefix="bft-base-"))
    worktree = tmp / "base"
    cases: List[JCase] = []
    setup_failed: Set[str] = set()
    failures_to_show: List[str] = []
    made_links: List[Path] = []
    git(root, "worktree", "add", "--detach", "--quiet", str(worktree), merge_base)
    try:
        for f in to_copy:
            dest = worktree / f
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / f, dest)
        for link in _links(root, rob, args.link):
            dest = worktree / link
            if dest.exists() or dest.is_symlink():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.symlink_to((root / link).resolve(), target_is_directory=(root / link).is_dir())
            made_links.append(dest)
        print(f"bft red-on-base: running {len(new)} new test(s) against {base_ref} ({merge_base[:9]})",
              file=sys.stderr)
        for idx, files in by_runner.items():
            runner = runners[idx]
            junit = tmp / f"junit-{idx}.xml"
            cmd = (runner["command"]
                   .replace("{files}", " ".join(shlex.quote(str(worktree / f)) for f in files))
                   .replace("{junit}", shlex.quote(str(junit))))
            proc = subprocess.run(cmd, shell=True, cwd=worktree / runner.get("cwd", "."), capture_output=True,
                                  text=True, env={**os.environ, "BFT_RED_ON_BASE": "1"})
            output = proc.stdout + proc.stderr
            if junit.exists():
                cases += parse_junit(junit)
            elif re.search(r"Error while loading conftest", output):
                # pytest aborts before writing a report when a conftest cannot be imported on base
                # (it uses something the branch adds): every new test behind it fails there.
                setup_failed.update(files)
            else:
                tail = (proc.stdout + proc.stderr).strip().splitlines()[-25:]
                failures_to_show.append(f"runner `{runner['name']}` wrote no JUnit XML (exit {proc.returncode}). "
                                        f"Command: {cmd}\n    " + "\n    ".join(tail))
    finally:
        for link in made_links:
            link.unlink()
        git(root, "worktree", "remove", "--force", str(worktree), check=False)
        git(root, "worktree", "prune", check=False)
        shutil.rmtree(tmp, ignore_errors=True)

    for msg in failures_to_show:
        print(f"bft red-on-base: {msg}", file=sys.stderr)
    passes = not_run = 0
    print(f"bft red-on-base: {len(new)} new test(s), run against {base_ref} @ {merge_base[:9]}")
    allowances = {rel: read_allowances(rel, (read_text(root / rel) or "").splitlines()) for rel, _ in new}
    for rel, t in new:
        hits = [] if rel in unassigned else _cases_for(rel, t, cases)
        statuses = {c.status for c in hits}
        label = f"{rel}::{t.name}" if kind_of(rel) == "py" else f"{rel} :: {t.title}"
        if rel in setup_failed:
            print(f"  red      {label}  (the test setup fails to load on base)")
        elif not hits and rel not in unassigned and _file_failed_to_load(rel, cases):
            print(f"  red      {label}  (the file fails to load on base)")
        elif not hits or statuses == {"skipped"}:
            not_run += 1
            why = ("no runner matches this file" if rel in unassigned else
                   "not in the JUnit report, or skipped; with pytest, pass --continue-on-collection-errors so one "
                   "file that cannot load on base does not stop the others")
            print(f"  NOT RUN  {label}  ({why})")
        elif "failed" in statuses:
            print(f"  red      {label}")
        else:
            reason = allowances[rel].reason("BFT010", max(1, t.start - 1), t.line)
            if reason:
                print(f"  allowed  {label}  (passes on base: {reason})")
            else:
                passes += 1
                print(f"  PASSES   {label}  (already passes on base; it does not pin the new behavior: BFT010)")
    if passes or (not_run and not args.allow_not_run):
        print("\nA test for the new behavior must fail before the change and pass after it. A test that already "
              "passes on the base branch either was written to fit the code, or guards behavior the change must "
              "not alter: an exception or boundary of the new rule (\"a voided cheque can still be re-entered\"), "
              "or a characterization test before a refactor. Guards are welcome; mark each one so a reviewer sees "
              "it is deliberate: `bft: allow BFT010 -- <what it guards>`.", file=sys.stderr)
        return 1
    return 0


IMPORT_RE = re.compile(r"^\s*(?:import\s|from\s+\S+\s+import\s|export\s+\{?.*\bfrom\s)")
FAILURE_RE = re.compile(r"\b(raise|except|throw|catch|reject|abort|HTTPException|Error\b|error|fail|invalid|denied|"
                        r"timeout|retry|rollback|status_code\s*=\s*[45]\d\d|status\(\s*[45]\d\d)", re.I)


def cmd_gaps(args, root: Path, cfg: dict) -> int:
    try:
        tree = ET.parse(args.coverage_xml)
    except (ET.ParseError, OSError) as e:
        print(f"bft gaps: cannot read {args.coverage_xml}: {e}", file=sys.stderr)
        return 2
    top = tree.getroot()
    sources = [s.text or "" for s in top.iter("source")] + [str(root)]
    changed = changed_files(root, args.changed_since) if args.changed_since else None
    source_cache: Dict[str, List[str]] = {}
    rows = []
    for cls in top.iter("class"):
        filename = cls.get("filename", "")
        rel, path = filename, None
        for src in sources:
            p = Path(src, filename)
            if p.exists():
                path = p.resolve()
                try:
                    rel = path.relative_to(root.resolve()).as_posix()
                except ValueError:
                    rel = filename
                break
        if changed is not None and rel not in changed:
            continue
        if rel not in source_cache:
            source_cache[rel] = (read_text(path) or "").splitlines() if path else []
        code = source_cache[rel]
        lines_xml = sorted(cls.iter("line"), key=lambda e: int(e.get("number", "0")))
        if lines_xml and all(int(e.get("hits", "0")) == 0 for e in lines_xml):
            rows.append((rel, 0, f"whole file never ran ({len(lines_xml)} lines)", False, ""))
            continue
        run: List[int] = []

        def flush() -> None:
            if run:
                snippet = [code[n - 1].strip() for n in run if 0 < n <= len(code)]
                where = f"{rel}:{run[0]}" + (f"-{run[-1]}" if len(run) > 1 else "")
                failure = any(FAILURE_RE.search(t) and not IMPORT_RE.match(t) for t in snippet)
                rows.append((where, run[0], "never ran", failure, snippet[0] if snippet else ""))
                run.clear()

        for line in lines_xml:
            num, hits = int(line.get("number", "0")), int(line.get("hits", "0"))
            if hits == 0:
                if run and num != run[-1] + 1:
                    flush()
                run.append(num)
                continue
            flush()
            cc = line.get("condition-coverage") or ""
            if line.get("branch") == "true" and cc and not cc.startswith("100%"):
                text = code[num - 1].strip() if 0 < num <= len(code) else ""
                taken = cc.split(" ", 1)[1].strip("()") if " " in cc else cc
                rows.append((f"{rel}:{num}", num, f"branch {taken} taken", bool(FAILURE_RE.search(text)), text))
        flush()

    branches_valid = int(top.get("branches-valid", "0") or 0)
    branch_rate = float(top.get("branch-rate", "0") or 0) * 100
    line_rate = float(top.get("line-rate", "0") or 0) * 100
    rows.sort(key=lambda r: (not r[3], r[0].split(":")[0], r[1]))
    print("# Coverage gaps\n")
    if branches_valid:
        print(f"Branch coverage **{branch_rate:.1f}%**, line coverage {line_rate:.1f}%.")
    else:
        print(f"Line coverage {line_rate:.1f}%. **Branch coverage is off**: enable it "
              "(coverage.py `branch = true`; the Istanbul/V8 providers report branches by default).")
    if changed is not None:
        print(f"Scope: files changed since `{args.changed_since}`.")
    print("\nAnswer the last column for every row: why is this not tested, and is that acceptable? "
          "Failure paths come first.\n")
    if rows:
        print("| # | Where | Missing | Failure path? | Code | Why untested |")
        print("|---|---|---|---|---|---|")
        for i, (where, _, what, failure, text) in enumerate(rows, 1):
            text = text.replace("|", "\\|").replace("`", "'")
            text = text[:77] + "..." if len(text) > 80 else text
            print(f"| {i} | `{where}` | {what} | {'yes' if failure else ''} | `{text}` | |")
    else:
        print("No gaps in scope.")
    if args.fail_under is not None:
        rate = branch_rate if branches_valid else line_rate
        if rate + 1e-9 < args.fail_under:
            kind = "branch" if branches_valid else "line"
            print(f"\nbft gaps: {kind} coverage {rate:.1f}% is under {args.fail_under:g}%.", file=sys.stderr)
            return 1
    return 0


def _replace(text: str, old: str, new: str, replace_all: bool) -> str:
    if not old:
        return text + new
    return text.replace(old, new) if replace_all else text.replace(old, new, 1)


def _after_edit(tool: str, ti: dict, current: str) -> Optional[str]:
    if tool == "Write":
        return ti.get("content", "")
    if tool == "Edit":
        return _replace(current, ti.get("old_string", ""), ti.get("new_string", ""), bool(ti.get("replace_all")))
    if tool == "MultiEdit":
        for e in ti.get("edits", []):
            current = _replace(current, e.get("old_string", ""), e.get("new_string", ""), bool(e.get("replace_all")))
        return current
    return None


def cmd_guard(args) -> int:
    """PreToolUse hook: block edits that change a committed (approved) scenario. Adding scenarios is fine."""
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    ti = event.get("tool_input") or {}
    raw = ti.get("file_path") or ti.get("notebook_path")
    if not raw:
        return 0
    cwd = Path(event.get("cwd") or os.getcwd())
    target = Path(raw) if os.path.isabs(raw) else cwd / raw
    probe = target.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    root = repo_root(probe)
    if not (root / CONFIG_NAME).exists():
        return 0  # opt-in: projects without the config are not touched
    cfg = load_config(root)
    if not cfg["guard"].get("enabled", True) or os.environ.get("BFT_UNLOCK_SCENARIOS") == "1":
        return 0
    try:
        rel = target.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return 0
    if not is_scenario_file(rel, cfg):
        return 0
    approved = {t.name for t in tests_in(rel, git_show(root, "HEAD", rel))}
    if not approved:
        return 0
    current = (read_text(target) or "") if target.exists() else ""
    after = _after_edit(event.get("tool_name", ""), ti, current)
    if after is None:
        return 0
    lines = current.splitlines()
    touched = [t for t in tests_in(rel, current)
               if t.name in approved and "\n".join(lines[t.start - 1:t.end]) not in after]
    if not touched:
        return 0
    names = "; ".join(t.title for t in touched[:5])
    sys.stderr.write(
        f"Blocked by behavior-first-testing: this edit changes approved scenario(s) in {rel}: {names}.\n"
        "Committed scenarios are the agreed behavior. Do not rewrite them to fit the code: if the code is wrong, "
        "fix the code. If the scenario itself is wrong, stop and ask the human. Show them the scenario, the change "
        "you propose, and why. They can make the change themselves, or restart Claude Code with "
        "BFT_UNLOCK_SCENARIOS=1. Adding new scenarios to this file is allowed.\n")
    return 2


INIT_TEMPLATE = """\
# Configuration for the behavior-first-testing skill (bft.py). Every key is optional.
# Globs are relative to the repository root; `**/` matches any number of directories.

# Files that hold tests or test support code (fixtures, fakes, helpers). Rules BFT001-BFT007 apply here.
test_globs = {test_globs}

# Acceptance scenarios: actor-first names, Given/When/Then, and frozen once committed (see [guard]).
scenario_globs = {scenario_globs}

# Scenario names start with one of these. `user` is the default; add others deliberately.
actors = ["user"]

# Checked for coverage exclusions without a reason (BFT008).
source_globs = {source_globs}
exclude_globs = {exclude_globs}

[guard]
# Block Claude Code from changing committed scenarios (needs the plugin hook or a hook in settings.json).
enabled = true

[red_on_base]
# New tests must fail on this branch's merge-base with `base`.
base = "{base}"
{runners}"""


def _detect_runners(root: Path) -> List[Tuple[str, str, str]]:
    found = []
    candidates = [root] + sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
    for d in candidates:
        if d.name in ("node_modules", "venv", "dist", "build"):
            continue
        rel = "." if d == root else d.relative_to(root).as_posix()
        glob = ["**"] if d == root else [f"{rel}/**"]
        pyproject = read_text(d / "pyproject.toml") or ""
        if "[tool.pytest" in pyproject or (d / "pytest.ini").exists() or (d / "conftest.py").exists() \
                or (d / "tests" / "conftest.py").exists():
            python = ".venv/bin/python" if (d / ".venv" / "bin" / "python").exists() else "python"
            found.append((rel, json.dumps(glob),
                          f"{python} -m pytest {{files}} -q -p no:cacheprovider --continue-on-collection-errors "
                          f"--junitxml={{junit}}"))
        package = read_text(d / "package.json") or ""
        if '"vitest"' in package:
            found.append((rel, json.dumps(glob), "npx vitest run {files} --reporter=junit --outputFile={junit}"))
        elif '"@playwright/test"' in package:
            found.append((rel, json.dumps(glob),
                          "PLAYWRIGHT_JUNIT_OUTPUT_NAME={junit} npx playwright test {files} --reporter=junit"))
        elif '"jest"' in package:
            found.append((rel, json.dumps(glob),
                          "JEST_JUNIT_OUTPUT_FILE={junit} npx jest {files} --reporters=jest-junit"))
    return found


def _default_base(root: Path) -> str:
    head = subprocess.run(["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], cwd=root,
                          capture_output=True, text=True)
    if head.returncode == 0 and head.stdout.strip():
        return head.stdout.strip()
    for ref in ("origin/main", "origin/master", "main", "master"):
        if subprocess.run(["git", "rev-parse", "--verify", "--quiet", ref], cwd=root,
                          capture_output=True).returncode == 0:
            return ref
    return DEFAULTS["red_on_base"]["base"]


def cmd_init(args, root: Path, cfg: dict) -> int:
    path = root / CONFIG_NAME
    if path.exists() and not args.force:
        print(f"bft init: {path} exists (use --force to overwrite).", file=sys.stderr)
        return 1
    runners = ""
    for rel, globs, command in _detect_runners(root):
        name = "root" if rel == "." else re.sub(r"\W+", "_", rel)
        runners += f"\n[red_on_base.runners.{name}]\nglobs = {globs}\ncwd = \"{rel}\"\ncommand = \"{command}\"\n"
    if not runners:
        runners = ("\n# [red_on_base.runners.backend]\n# globs = [\"backend/**\"]\n# cwd = \"backend\"\n"
                   "# command = \".venv/bin/python -m pytest {files} -q -p no:cacheprovider "
                   "--continue-on-collection-errors --junitxml={junit}\"\n")
    body = INIT_TEMPLATE.format(
        test_globs=json.dumps(DEFAULTS["test_globs"]), scenario_globs=json.dumps(DEFAULTS["scenario_globs"]),
        source_globs=json.dumps(DEFAULTS["source_globs"]), exclude_globs=json.dumps(DEFAULTS["exclude_globs"]),
        base=_default_base(root), runners=runners)
    path.write_text(body, encoding="utf-8")
    print(f"bft init: wrote {path}. Review the globs and runners, then run `bft.py lint`.")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="bft.py", description="Checks for the behavior-first-testing skill.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("lint", help="check tests against the rules")
    s.add_argument("paths", nargs="*")
    s.add_argument("--changed-since", metavar="REF", help="only files changed since the merge-base with REF")
    s.add_argument("--format", choices=["text", "github"], default="text")
    s = sub.add_parser("scenarios", help="list scenarios as Given / When / Then")
    s.add_argument("paths", nargs="*")
    s = sub.add_parser("red-on-base", help="new tests must fail on the base branch")
    s.add_argument("--base", metavar="REF")
    s.add_argument("--command", help="runner command with {files} and {junit} placeholders")
    s.add_argument("--cwd", help="directory for --command, relative to the repository root")
    s.add_argument("--link", action="append", default=[], metavar="PATH",
                   help="also lend this path (e.g. an ignored .env) from your checkout to the base checkout; repeatable")
    s.add_argument("--allow-not-run", action="store_true", help="do not fail when a new test could not be run")
    s = sub.add_parser("gaps", help="uncovered lines and branches from a Cobertura coverage.xml")
    s.add_argument("coverage_xml")
    s.add_argument("--changed-since", metavar="REF")
    s.add_argument("--fail-under", type=float, metavar="PCT")
    sub.add_parser("guard", help="Claude Code PreToolUse hook")
    s = sub.add_parser("init", help="write a starter .behavior-testing.toml")
    s.add_argument("--force", action="store_true")
    sub.add_parser("rules", help="list the rule ids")
    args = p.parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)
    if args.cmd == "guard":
        return cmd_guard(args)
    root = repo_root(Path.cwd())
    cfg = load_config(root)
    handler = {"lint": cmd_lint, "scenarios": cmd_scenarios, "red-on-base": cmd_red_on_base,
               "gaps": cmd_gaps, "init": cmd_init, "rules": cmd_rules}[args.cmd]
    return handler(args, root, cfg)


if __name__ == "__main__":
    sys.exit(main())
