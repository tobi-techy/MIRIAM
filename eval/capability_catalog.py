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
through pytest and reports *every* area the policy governs with an explicit
state, so an area can never be certified by being absent from the report:

  * ``PROVEN``  -- the area has claimed evidence and enough of it passed.
  * ``PENDING`` -- the area is unmeasured, or its claimed evidence failed. An
    unmeasured trust area in this state holds the deploy.
  * ``EXEMPT``  -- the area is unmeasured and a checked-in exemption
    (``eval/gate_exemptions.json``) covers it, carrying a justification, an
    owner, a review-by date and the paths that would invalidate it.

A skip or an error in evidence counts as not-passed, and a whole-file evidence
entry only passes when every test pytest collected from that file ran and
passed. A capability is only as proven as its evidence: promoting one means
adding a test that exercises production code.

Run it directly for the report (exit 1 means HOLD)::

    uv run python -m eval.capability_catalog
    uv run python -m eval.capability_catalog --list
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Collection, Mapping, Sequence
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = ROOT / "eval" / "capabilities.json"
EXEMPTIONS_PATH = ROOT / "eval" / "gate_exemptions.json"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval.capability_policy import TRUST_AREAS, required_pass_rate  # noqa: E402


class Status(StrEnum):
    PLANNED = "planned"
    UNPROVEN = "unproven"
    PARTIAL = "partial"
    LIVE = "live"


class AreaState(StrEnum):
    PROVEN = "PROVEN"
    PENDING = "PENDING"
    EXEMPT = "EXEMPT"


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


class Exemption(BaseModel):
    """One area's checked-in claim that it is not built and not measured.

    ``invalidating_paths`` is the tripwire: a module or glob that must not
    exist for the exemption to stand. The day someone builds the area at one
    of those paths, the gate fails on its own.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    area: str
    justification: str
    owner: str
    review_by: date
    invalidating_paths: tuple[str, ...] = Field(min_length=1)


class Exemptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str
    exemptions: tuple[Exemption, ...]


def load_catalog(path: Path = CATALOG_PATH) -> Catalog:
    return Catalog.model_validate(json.loads(path.read_text(encoding="utf-8")))


def load_exemptions(path: Path = EXEMPTIONS_PATH) -> Exemptions:
    """Read the checked-in exemptions; absent means none, which holds the gate."""
    if not path.is_file():
        return Exemptions(version="", exemptions=())
    return Exemptions.model_validate(json.loads(path.read_text(encoding="utf-8")))


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
    """pytest plugin recording every collected test and its outcome.

    A test counts as passed only when its ``call`` phase passed. A skip -- which
    pytest reports in the ``setup`` phase -- and a failure anywhere are recorded
    as not-passed, so a skipped test can never prove a capability.
    """

    def __init__(self) -> None:
        self.outcomes: dict[str, bool] = {}
        self.collected: set[str] = set()

    def pytest_collection_modifyitems(self, items) -> None:  # type: ignore[no-untyped-def]
        self.collected.update(item.nodeid for item in items)

    def pytest_runtest_logreport(self, report) -> None:  # type: ignore[no-untyped-def]
        if report.skipped or report.failed:
            self.outcomes[report.nodeid] = False
        elif report.when == "call":
            self.outcomes[report.nodeid] = report.passed


def _passed(
    node: str,
    outcomes: Mapping[str, bool],
    collected: Collection[str],
) -> bool:
    """True only when every test under ``node`` was collected and passed.

    ``node`` may be a whole file, a class, or a single test. Every test pytest
    collected under it must have run and passed; a test that skipped or errored
    counts as not-passed, so whole-file evidence cannot pass while a test skips.
    """
    under = [
        nid
        for nid in collected
        if nid == node or nid.startswith(f"{node}::") or nid.startswith(f"{node}[")
    ]
    return bool(under) and all(outcomes.get(nid) is True for nid in under)


def run_evidence(nodes: Sequence[str]) -> _Collector:
    """Run pytest over ``nodes`` and return what it recorded."""
    import pytest

    collector = _Collector()
    if nodes:
        pytest.main(
            ["-q", "-p", "no:cacheprovider", "--no-header", *nodes],
            plugins=[collector],
        )
    return collector


def _path_exists(root: Path, pattern: str) -> bool:
    if any(ch in pattern for ch in "*?["):
        return next(root.glob(pattern), None) is not None
    return (root / pattern).exists()


def check_exemptions(
    exemptions: Exemptions,
    areas: Collection[str],
    *,
    root: Path = ROOT,
    today: date | None = None,
) -> tuple[list[str], dict[str, Exemption]]:
    """Validate every exemption; return the problems and the ones that stand."""
    today = today or date.today()
    errors: list[str] = []
    valid: dict[str, Exemption] = {}
    for ex in exemptions.exemptions:
        if ex.area in valid:
            errors.append(f"more than one exemption for {ex.area!r}")
            continue
        if ex.area not in areas:
            errors.append(f"exemption for unknown area {ex.area!r}")
            continue
        built = [p for p in ex.invalidating_paths if _path_exists(root, p)]
        if built:
            errors.append(
                f"exemption for {ex.area!r} is invalidated by an existing path: "
                f"{', '.join(built)}"
            )
            continue
        if ex.review_by < today:
            errors.append(
                f"exemption for {ex.area!r} expired on {ex.review_by.isoformat()}"
            )
            continue
        valid[ex.area] = ex
    return errors, valid


class AreaResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    area: str
    state: AreaState
    passed: int
    total: int
    catalogued: int
    required: float
    failures: tuple[str, ...] = ()
    exemption: Exemption | None = None

    @property
    def rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    @property
    def blocks(self) -> bool:
        """Whether this area holds the deploy.

        A trust area holds when it is not proven, even with no claimed
        evidence; a quality area holds only when claimed evidence failed.
        """
        if self.state is not AreaState.PENDING:
            return False
        return self.area in TRUST_AREAS or self.total > 0


class GateReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    areas: tuple[AreaResult, ...]
    errors: tuple[str, ...] = ()

    @property
    def holds(self) -> bool:
        return bool(self.errors) or any(a.blocks for a in self.areas)

    @property
    def exit_code(self) -> int:
        return 1 if self.holds else 0


def run_gate(
    catalog: Catalog,
    exemptions: Exemptions | None = None,
    *,
    collector: _Collector | None = None,
    root: Path = ROOT,
    today: date | None = None,
) -> GateReport:
    """Evaluate every policy area and decide whether the deploy holds.

    ``collector`` may be supplied to evaluate against a run made elsewhere;
    otherwise the evidence for every claimed-proven capability is run here.
    """
    today = today or date.today()
    exemptions = exemptions or Exemptions(version="", exemptions=())
    areas = {c.area for c in catalog.capabilities} | set(TRUST_AREAS)
    errors, valid = check_exemptions(exemptions, areas, root=root, today=today)

    proven = [c for c in catalog.capabilities if c.status in PROVEN]
    if collector is None:
        nodes = sorted({n for c in proven for n in c.evidence})
        collector = run_evidence(nodes)

    by_area: dict[str, list[tuple[Capability, bool]]] = defaultdict(list)
    for cap in proven:
        ok = bool(cap.evidence) and all(
            _passed(n, collector.outcomes, collector.collected) for n in cap.evidence
        )
        by_area[cap.area].append((cap, ok))

    catalogued = Counter(c.area for c in catalog.capabilities)
    results: list[AreaResult] = []
    for area in sorted(areas):
        caps = by_area.get(area, [])
        passed = sum(ok for _, ok in caps)
        total = len(caps)
        required = required_pass_rate(area)
        if total and passed / total >= required:
            state = AreaState.PROVEN
        elif not total and area in valid:
            state = AreaState.EXEMPT
        else:
            state = AreaState.PENDING
        results.append(
            AreaResult(
                area=area,
                state=state,
                passed=passed,
                total=total,
                catalogued=catalogued[area],
                required=required,
                failures=tuple(f"{c.id} {c.name}" for c, ok in caps if not ok),
                exemption=valid.get(area) if state is AreaState.EXEMPT else None,
            )
        )
    return GateReport(areas=tuple(results), errors=tuple(errors))


def _print_report(report: GateReport) -> None:
    print("\nCapability gate")
    for r in report.areas:
        note = ""
        if r.state is AreaState.PENDING and not r.blocks:
            note = "  [quality area, not gating]"
        print(
            f"  {r.state.value:<7} {r.area}: {r.passed}/{r.total} checked "
            f"of {r.catalogued} catalogued (needs {r.required:.0%}){note}"
        )
        for failure in r.failures:
            print(f"        failing: {failure}")

    exempted = [r for r in report.areas if r.state is AreaState.EXEMPT]
    if exempted:
        print("\nExempted areas (not measured; deploy holds unless this stands)")
        for r in exempted:
            ex = r.exemption
            assert ex is not None
            print(f"  {r.area}: review by {ex.review_by.isoformat()}, owner {ex.owner}")
            print(f"    why: {ex.justification}")
            print("    invalidated if any of these paths exists:")
            for pattern in ex.invalidating_paths:
                print(f"      {pattern}")

    if report.errors:
        print("\nHOLD: exemption problems")
        for error in report.errors:
            print(f"  {error}")
    print("\nHOLD" if report.holds else "\nSHIP")


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

    report = run_gate(catalog, load_exemptions())
    _print_report(report)
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
