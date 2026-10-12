#!/usr/bin/env python
"""A ratchet for mypy: no new type errors, and the baseline only shrinks.

``mypy miriam_agent`` reports ~150 pre-existing errors (mixin attribute
resolution, untyped returns, SQLAlchemy declarative typing). Gating on zero
would mean the gate can never pass, so nothing would be checked at all. This
script instead compares the current run against a recorded baseline and fails
only when a new error appears, the same shape as the blob ratchet in
``tests/architecture/test_module_rules.py``.

Matching is on ``(path, rule, message)`` and not on the line number, so moving
code around does not manufacture phantom "new" errors. Baseline entries are a
multiset: an extra copy of a known error is still a new error.

Usage::

    python scripts/mypy_ratchet.py            # check (exit 1 on a new error)
    python scripts/mypy_ratchet.py --write    # rewrite the baseline
    python scripts/mypy_ratchet.py --update   # accept removals, reject additions
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "mypy-baseline.json"
TARGET = "miriam_agent"

# path:line: error: message  [rule]
_LINE = re.compile(r"^(?P<path>[^:]+):\d+: (?P<sev>error|note): (?P<msg>.*)$")


def _key(path: str, msg: str) -> str:
    rule = ""
    match = re.search(r"\s+\[(?P<rule>[a-z-]+)\]$", msg)
    if match:
        rule = match.group("rule")
        msg = msg[: match.start()]
    return f"{path}|{rule}|{msg}"


def collect() -> Counter[str]:
    proc = subprocess.run(
        [sys.executable, "-m", "mypy", TARGET],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    errors: Counter[str] = Counter()
    for line in proc.stdout.splitlines():
        match = _LINE.match(line)
        if match and match.group("sev") == "error":
            errors[_key(match.group("path"), match.group("msg"))] += 1
    return errors


def load() -> Counter[str]:
    if not BASELINE.exists():
        return Counter()
    return Counter(json.loads(BASELINE.read_text()))


def write(errors: Counter[str]) -> None:
    BASELINE.write_text(json.dumps(dict(sorted(errors.items())), indent=2) + "\n")


def _describe(entries: list[str]) -> str:
    return "\n".join(f"  {e}  (x{n})" if n > 1 else f"  {e}" for e, n in entries)


def main(argv: list[str]) -> int:
    current = collect()
    baseline = load()

    new = sorted(
        ((key, n) for key, n in current.items() if n > baseline.get(key, 0)),
        key=lambda item: item[0],
    )
    fixed = sorted(
        ((key, n) for key, n in baseline.items() if n > current.get(key, 0)),
        key=lambda item: item[0],
    )

    if "--write" in argv:
        write(current)
        print(f"wrote {sum(current.values())} errors to {BASELINE.name}")
        return 0

    if fixed:
        print(f"Resolved since the baseline ({len(fixed)} kinds):")
        print(_describe(fixed))
        if "--update" in argv:
            write(current)
            print(f"baseline tightened to {sum(current.values())} errors")
            return 0
        print("Re-run with --update to lock in the improvement.")

    if new:
        print(f"New mypy errors ({sum(n for _, n in new)}):")
        print(_describe(new))
        print(
            "\nFix them, or if a fix is genuinely out of scope record it with "
            "`python scripts/mypy_ratchet.py --write`."
        )
        return 1

    print(f"mypy ratchet holds: {sum(current.values())} known errors, none new.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
