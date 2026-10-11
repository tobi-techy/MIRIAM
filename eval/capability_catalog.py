"""Miriam's capability catalog and the deploy gate built on it.

``eval/capabilities.json`` lists every capability a shop agent needs, each with
its eval (scenario, expected behaviour, pass rule, target). Each one also
records how far it really is:

  * ``planned``  -- not built. Nothing is claimed, so nothing is run.
  * ``unproven`` -- code exists but no passing test proves it. Listed so the
    gap stays visible; not run.
  * ``partial``  -- some behaviour is proven by the listed tests; ``gap`` says
    what is not.
  * ``live``     -- fully proven by the listed tests.

The gate runs the evidence for every ``partial`` and ``live`` capability
through pytest and holds the deploy when an area falls below its bar in
``eval/capability_policy.py``. A capability is only as proven as its
evidence: promoting one means adding a test that exercises production code.

Run it directly for the report (exit 1 means HOLD)::

    uv run python -m eval.capability_catalog
    uv run python -m eval.capability_catalog --list
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import defaultdict
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = ROOT / "eval" / "capabilities.json"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval.capability_policy import required_pass_rate  # noqa: E402


class Status(StrEnum):
    PLANNED = "planned"
    UNPROVEN = "unproven"
    PARTIAL = "partial"
    LIVE = "live"


PROVEN = frozenset({Status.PARTIAL, Status.LIVE})


class EvalSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario: str
    expected: str
    pass_rule: str
    target: str
    method: str


class Capability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^C\d{3}$")
    area: str
    name: str
    does: str
    priority: Literal["P0", "P1", "P2"]
    phase: Literal["pilot", "scale"]
    status: Status
    evidence: tuple[str, ...] = ()
    gap: str = ""
    eval: EvalSpec


class Catalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str
    capabilities: tuple[Capability, ...]


def load_catalog(path: Path = CATALOG_PATH) -> Catalog:
    return Catalog.model_validate(json.loads(path.read_text(encoding="utf-8")))


def _test_names(path: Path) -> set[str]:
    """Every ``test_*`` function in a file, as ``name`` or ``Class::name``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.add(f"{node.name}::{item.name}")
    return {n for n in names if n.split("::")[-1].startswith("test_")}


def missing_evidence(catalog: Catalog) -> list[str]:
    """Evidence node ids that do not resolve to a real test in the repo."""
    missing: list[str] = []
    for cap in catalog.capabilities:
        for node in cap.evidence:
            file_part, _, test_part = node.partition("::")
            path = ROOT / file_part
            if not path.is_file():
                missing.append(f"{cap.id}: {node}")
            elif test_part and test_part not in _test_names(path):
                missing.append(f"{cap.id}: {node}")
    return missing


class _Collector:
    """pytest plugin that records the outcome of every test it sees."""

    def __init__(self) -> None:
        self.outcomes: dict[str, bool] = {}

    def pytest_runtest_logreport(self, report) -> None:  # type: ignore[no-untyped-def]
        if report.when == "call" or (report.when == "setup" and report.failed):
            self.outcomes[report.nodeid] = report.passed


def _passed(node: str, outcomes: dict[str, bool]) -> bool:
    """A node id passes when every test under it ran and passed."""
    hits = [
        ok for nid, ok in outcomes.items() if nid == node or nid.startswith(node + "::")
    ]
    return bool(hits) and all(hits)


class AreaResult(BaseModel):
    area: str
    passed: int
    total: int
    required: float
    failures: list[str]

    @property
    def rate(self) -> float:
        return self.passed / self.total if self.total else 1.0

    @property
    def ships(self) -> bool:
        return self.rate >= self.required


def run_gate(catalog: Catalog) -> list[AreaResult]:
    import pytest

    proven = [c for c in catalog.capabilities if c.status in PROVEN]
    nodes = sorted({n for c in proven for n in c.evidence})
    collector = _Collector()
    pytest.main(
        ["-q", "-p", "no:cacheprovider", "--no-header", *nodes], plugins=[collector]
    )

    by_area: dict[str, list[tuple[Capability, bool]]] = defaultdict(list)
    for cap in proven:
        ok = bool(cap.evidence) and all(
            _passed(n, collector.outcomes) for n in cap.evidence
        )
        by_area[cap.area].append((cap, ok))
    return [
        AreaResult(
            area=area,
            passed=sum(ok for _, ok in caps),
            total=len(caps),
            required=required_pass_rate(area),
            failures=[f"{c.id} {c.name}" for c, ok in caps if not ok],
        )
        for area, caps in sorted(by_area.items())
    ]


def _print_list(catalog: Catalog) -> None:
    counts: dict[str, int] = defaultdict(int)
    for cap in catalog.capabilities:
        counts[cap.status.value] += 1
        if cap.status is not Status.PLANNED:
            print(f"{cap.id} [{cap.status.value}] {cap.name}")
            if cap.gap:
                print(f"    gap: {cap.gap}")
    print("\n" + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--list", action="store_true", help="show status, run nothing")
    args = parser.parse_args(argv)

    catalog = load_catalog()
    if args.list:
        _print_list(catalog)
        return 0

    broken = missing_evidence(catalog)
    if broken:
        print("HOLD: evidence points at tests that do not exist:")
        print("\n".join(f"  {b}" for b in broken))
        return 1

    results = run_gate(catalog)
    print("\nCapability gate")
    for r in results:
        verdict = "SHIP" if r.ships else "HOLD"
        print(f"  {verdict}  {r.area}: {r.passed}/{r.total} (needs {r.required:.0%})")
        for f in r.failures:
            print(f"        failing: {f}")
    held = [r for r in results if not r.ships]
    print("\nHOLD" if held else "\nSHIP")
    return 1 if held else 0


if __name__ == "__main__":
    raise SystemExit(main())
