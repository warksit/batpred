#!/usr/bin/env python3
"""Guard: every `def test_*` must actually be wired into its runner.

The #1 documented way a green suite lies is a test that never ran at all —
written, never added to the runner list, executed zero times, and indistinguishable
from a passing test because the total just quietly stays the same.

It happened twice on 2026-08-10/12 and twice more while writing RD40/RD33 on
2026-08-13, where two new tests had to be hand-added to their runner tuples after
the fact. A rule cannot fix that (you have to remember to read it); this can.

The harnesses use four different runner shapes — `main()`, `run_<x>_tests()`, a
bare `if __name__` block, and a `TEST_REGISTRY` dict — so this does NOT try to
execute them. It asserts each test NAME is referenced somewhere other than its own
`def` line. That is shape-agnostic and catches the real defect.

Run: cd apps/predbat && python3 tests/test_all_tests_registered.py
"""

import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# OUR files only — let Predbat test Predbat.
#
# Upstream test modules are overwritten on update, so "fixing" their wiring is
# churn that a Predbat release silently undoes, and their hygiene is not ours to
# police. Scoping to our own tree also keeps the guard fast and its failures
# actionable. (Run wide once on 2026-08-13 it found exactly one orphan upstream,
# `test_kraken.test_run_does_not_wire_export_when_rates_unavailable` — genuinely
# never executed, and deliberately left alone.)
#
# The REFERENCE corpus stays wide (see _corpus) so a test of ours registered in
# unit_test.py still counts as wired.
OURS = (
    "test_curtailment.py",
    "test_yaml_*.py",
    "test_requirements_implemented.py",
    "test_soc_keep_publish.py",
    "test_plugin_host_contract.py",
)

DEF_RE = re.compile(r"^def (test_[A-Za-z0-9_]+)\s*\(", re.M)


def _strip_comments(line):
    """Drop trailing comments so a name mentioned only in prose does not count."""
    out, in_s, quote = [], False, ""
    for i, ch in enumerate(line):
        if in_s:
            out.append(ch)
            if ch == quote and line[i - 1 : i] != "\\":
                in_s = False
            continue
        if ch in "\"'":
            in_s, quote = True, ch
            out.append(ch)
            continue
        if ch == "#":
            break
        out.append(ch)
    return "".join(out)


def _corpus(extra_paths=()):
    """Every place a test could legitimately be wired from.

    NOT just the test's own file: upstream Predbat tests are registered in
    `unit_test.py`'s TEST_REGISTRY, an EXTERNAL runner. The first version of this
    guard checked only the defining file and reported 80 false positives — the
    same wrong-fixture-assumption it exists to catch, which is why the
    self-check below now pins BOTH shapes.
    """
    paths = list(glob.glob(os.path.join(HERE, "*.py")))
    paths.append(os.path.join(HERE, "..", "unit_test.py"))
    paths.extend(extra_paths)
    out = {}
    for p in paths:
        if os.path.exists(p):
            with open(p) as fh:
                out[os.path.abspath(p)] = fh.read()
    return out


IDENT_RE = re.compile(r"\b(test_[A-Za-z0-9_]+)\b")
DEF_LINE_RE = re.compile(r"^def test_[A-Za-z0-9_]+\s*\(")


def _referenced_names(corpus):
    """Every `test_*` identifier that appears somewhere OTHER than a `def` line.

    Single pass over the corpus — the first version compared every name against
    every line of every file and took over two minutes. A guard nobody waits for
    is a guard nobody runs.
    """
    referenced = set()
    for text in corpus.values():
        for line in text.splitlines():
            # Cheap reject first: _strip_comments is a per-character loop, and
            # the overwhelming majority of lines cannot contain a test name.
            # Without this the guard took 46 s; with it, under 2 s.
            if "test_" not in line:
                continue
            if DEF_LINE_RE.match(line):
                continue  # a definition is not a reference
            referenced.update(IDENT_RE.findall(_strip_comments(line)))
    return referenced


def _unregistered(path, corpus=None):
    """Test names defined in `path` that nothing anywhere calls."""
    with open(path) as fh:
        names = DEF_RE.findall(fh.read())
    corpus = _corpus() if corpus is None else corpus
    referenced = _referenced_names(corpus)
    return names, [n for n in names if n not in referenced]


def test_every_test_is_registered():
    """No test may be defined without being wired into a runner."""
    targets = sorted({p for pattern in OURS for p in glob.glob(os.path.join(HERE, pattern))})
    assert targets, "OURS matched no files — the guard would vacuously pass"
    corpus = _corpus()
    checked = failures = total = 0
    for path in targets:
        base = os.path.basename(path)
        if base == os.path.basename(__file__):
            continue
        names, missing = _unregistered(path, corpus)
        if not names:
            continue
        checked += 1
        total += len(names)
        if missing:
            failures += len(missing)
            print("  FAIL {}: {} test(s) defined but never referenced by a runner:".format(base, len(missing)))
            for m in missing:
                print("         - {}".format(m))
    assert failures == 0, "{} test(s) are defined but never run — they prove nothing".format(failures)
    print("  test_every_test_is_registered: PASSED ({} tests across {} files, all wired)".format(total, checked))


def test_the_guard_itself_detects_an_unwired_test():
    """The guard must FAIL on a file containing an unreferenced test.

    Without this the guard could silently stop working — a checker that can only
    pass is the same class of defect it exists to catch (Charter: a green test
    proves nothing until you know it RAN and REACHED its subject).
    """
    import tempfile

    src = 'def test_wired_locally():\n    pass\n\n\ndef test_wired_externally():\n    pass\n\n\ndef test_orphan():\n    pass\n\n\nif __name__ == "__main__":\n    test_wired_locally()\n'
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write(src)
        tmp = fh.name
    # An EXTERNAL runner, i.e. the unit_test.py TEST_REGISTRY shape.
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write('TEST_REGISTRY = {"x": test_wired_externally}\n')
        ext = fh.name
    try:
        corpus = {tmp: open(tmp).read(), ext: open(ext).read()}
        names, missing = _unregistered(tmp, corpus)
        assert set(names) == {"test_wired_locally", "test_wired_externally", "test_orphan"}, names
        # Both wiring shapes must count as wired; only the orphan may be flagged.
        assert missing == ["test_orphan"], "guard must flag exactly the unwired test, got {}".format(missing)
    finally:
        os.unlink(tmp)
        os.unlink(ext)
    print("  test_the_guard_itself_detects_an_unwired_test: PASSED (local + external wiring both count; orphan flagged)")


def main():
    """Run the registration guard."""
    for t in (
        test_the_guard_itself_detects_an_unwired_test,
        test_every_test_is_registered,
    ):
        t()
    print("test_all_tests_registered: ALL PASSED")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as exc:
        print("FAIL — {}".format(exc))
        sys.exit(1)
