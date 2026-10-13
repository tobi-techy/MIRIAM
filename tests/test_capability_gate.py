"""The deploy gate's own evidence.

The gate only earns its name if it refuses to certify what it has not
measured. These tests prove that refusal: an unmeasured trust area is PENDING
and holds the deploy, every policy area appears in the report, a valid
exemption makes an unmeasured area EXEMPT and lets the deploy ship, an
exemption whose invalidating path exists or whose review-by date has passed
fails, a skipped or failing test cannot prove a whole-file evidence entry, and
a genuinely proven area still yields SHIP.
"""

from __future__ import annotations

import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from eval.capability_catalog import (  # noqa: E402
    AreaState,
    Capability,
    Catalog,
    EvalSpec,
    Exemption,
    Exemptions,
    GateReport,
    Status,
    _passed,
    check_exemptions,
    load_catalog,
    load_exemptions,
    run_evidence,
    run_gate,
)
from eval.capability_policy import TRUST_AREAS  # noqa: E402

# A fixed "today" keeps the exemption clocks deterministic: the checked-in
# review-by dates must be in the future of this date for the tests to mean
# anything, and they are.
TODAY = date(2026, 10, 13)
LATER = date(2099, 1, 1)
BEFORE = date(2000, 1, 1)
ABSENT = ("miriam_agent/this_module_does_not_exist*",)


def _capability(
    cid: str,
    area: str,
    status: Status,
    evidence: tuple[str, ...] = (),
) -> Capability:
    return Capability(
        id=cid,
        area=area,
        name="a test capability",
        does="does a testable thing",
        priority="P0",
        phase="pilot",
        status=status,
        evidence=evidence,
        gap="the test gap",
        eval=EvalSpec(
            scenario="scenario",
            expected="expected",
            pass_rule="pass rule",
            target="target",
            method="method",
        ),
    )


def _catalog(*capabilities: Capability) -> Catalog:
    return Catalog(version="test", capabilities=tuple(capabilities))


class FakeCollector:
    """A pytest run's results, supplied instead of executed."""

    def __init__(self, outcomes: dict[str, bool]) -> None:
        self.outcomes = dict(outcomes)
        self.collected = set(outcomes)


def _exemption(area: str, review_by: date, paths: tuple[str, ...]) -> Exemption:
    return Exemption(
        area=area,
        justification="test justification",
        owner="test owner",
        review_by=review_by,
        invalidating_paths=paths,
    )


def _exempt_trust(review_by: date, paths: tuple[str, ...] = ABSENT) -> Exemptions:
    return Exemptions(
        version="test",
        exemptions=tuple(
            _exemption(area, review_by, paths) for area in sorted(TRUST_AREAS)
        ),
    )


def _area(report: GateReport, area: str):
    return next(result for result in report.areas if result.area == area)


def _plant(tmp_path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return str(path)


def _file_node(collector) -> str:
    """The file-level node id pytest used for the (only) planted file."""
    return next(iter(collector.outcomes)).split("::")[0]


def test_an_unmeasured_trust_area_is_pending_and_holds_the_deploy():
    catalog = _catalog(_capability("C001", "Conversation & Language", Status.PLANNED))
    report = run_gate(catalog, collector=FakeCollector({}), today=LATER)
    credit = _area(report, "Credit & Distributors")
    assert credit.state is AreaState.PENDING
    assert credit.blocks
    assert report.exit_code != 0


def test_every_policy_area_appears_in_the_report():
    catalog = _catalog(_capability("C001", "Conversation & Language", Status.PLANNED))
    report = run_gate(catalog, collector=FakeCollector({}), today=LATER)
    reported = {result.area for result in report.areas}
    assert TRUST_AREAS <= reported
    assert "Conversation & Language" in reported


def test_an_exempted_unmeasured_trust_area_ships():
    catalog = _catalog(_capability("C060", "Credit & Distributors", Status.PLANNED))
    report = run_gate(
        catalog, _exempt_trust(LATER), collector=FakeCollector({}), today=TODAY
    )
    credit = _area(report, "Credit & Distributors")
    assert credit.state is AreaState.EXEMPT
    assert not credit.blocks
    assert report.exit_code == 0


def test_an_exemption_is_invalidated_when_its_paths_exist():
    catalog = _catalog(_capability("C026", "Inventory", Status.PLANNED))
    exemptions = Exemptions(
        version="test",
        exemptions=(_exemption("Inventory", LATER, ("miriam_agent/hands/ledger.py",)),),
    )
    report = run_gate(catalog, exemptions, collector=FakeCollector({}), today=TODAY)
    inventory = _area(report, "Inventory")
    assert inventory.state is AreaState.PENDING
    assert any("invalidated" in error for error in report.errors)
    assert report.exit_code != 0


def test_an_expired_exemption_fails():
    catalog = _catalog(_capability("C038", "Sales Records", Status.PLANNED))
    exemptions = Exemptions(
        version="test",
        exemptions=(_exemption("Sales Records", BEFORE, ABSENT),),
    )
    report = run_gate(catalog, exemptions, collector=FakeCollector({}), today=TODAY)
    sales = _area(report, "Sales Records")
    assert sales.state is AreaState.PENDING
    assert any("expired" in error for error in report.errors)
    assert report.exit_code != 0


def test_an_exemption_for_an_unknown_area_fails():
    catalog = _catalog(_capability("C001", "Conversation & Language", Status.PLANNED))
    exemptions = Exemptions(
        version="test",
        exemptions=(_exemption("Not An Area", LATER, ABSENT),),
    )
    report = run_gate(catalog, exemptions, collector=FakeCollector({}), today=TODAY)
    assert any("unknown area" in error for error in report.errors)
    assert report.exit_code != 0


def test_a_quality_area_with_failing_evidence_holds_the_deploy():
    catalog = _catalog(
        _capability(
            "C001",
            "Conversation & Language",
            Status.PARTIAL,
            evidence=("tests/planted.py::test_a",),
        )
    )
    report = run_gate(
        catalog,
        _exempt_trust(LATER),
        collector=FakeCollector({"tests/planted.py::test_a": False}),
        today=TODAY,
    )
    conversation = _area(report, "Conversation & Language")
    assert conversation.state is AreaState.PENDING
    assert conversation.blocks
    assert report.exit_code != 0


def test_evidence_that_never_ran_is_not_proven():
    assert _passed("tests/test_smoke.py", {}, set()) is False
    assert _passed("tests/test_smoke.py::never_collected", {}, set()) is False


def test_a_whole_file_evidence_entry_with_a_skipped_test_is_not_proven(tmp_path):
    planted = _plant(
        tmp_path,
        "test_planted_evidence.py",
        "import pytest\n\n\n"
        "def test_it_runs():\n"
        "    assert True\n\n\n"
        "def test_it_skips():\n"
        "    pytest.skip('planted skip')\n",
    )
    collector = run_evidence([planted])
    node = _file_node(collector)
    assert f"{node}::test_it_skips" in collector.collected
    assert collector.outcomes[f"{node}::test_it_skips"] is False
    assert _passed(node, collector.outcomes, collector.collected) is False

    catalog = _catalog(_capability("C026", "Inventory", Status.PARTIAL, (node,)))
    report = run_gate(catalog, _exempt_trust(LATER), collector=collector, today=TODAY)
    inventory = _area(report, "Inventory")
    assert inventory.state is AreaState.PENDING
    assert report.exit_code != 0


def test_a_whole_file_evidence_entry_with_a_failing_test_is_not_proven(tmp_path):
    planted = _plant(
        tmp_path,
        "test_planted_failure.py",
        "def test_it_runs():\n"
        "    assert True\n\n\n"
        "def test_it_fails():\n"
        "    assert False\n",
    )
    collector = run_evidence([planted])
    node = _file_node(collector)
    assert _passed(node, collector.outcomes, collector.collected) is False


def test_a_genuinely_proven_area_still_ships(tmp_path):
    planted = _plant(
        tmp_path,
        "test_planted_clean.py",
        "def test_one():\n    assert True\n\n\ndef test_two():\n    assert True\n",
    )
    collector = run_evidence([planted])
    node = _file_node(collector)
    assert _passed(node, collector.outcomes, collector.collected) is True

    catalog = _catalog(
        _capability("C001", "Conversation & Language", Status.PARTIAL, (node,))
    )
    report = run_gate(catalog, _exempt_trust(LATER), collector=collector, today=TODAY)
    conversation = _area(report, "Conversation & Language")
    assert conversation.state is AreaState.PROVEN
    assert not conversation.blocks
    assert report.exit_code == 0


def test_the_checked_in_exemptions_stand_and_cover_only_the_four_areas():
    catalog = load_catalog()
    exemptions = load_exemptions()
    areas = {cap.area for cap in catalog.capabilities} | set(TRUST_AREAS)
    errors, valid = check_exemptions(exemptions, areas, today=TODAY)
    assert errors == []
    assert set(valid) == {
        "Credit & Distributors",
        "Expenses & Cash",
        "Inventory",
        "Sales Records",
    }
    for exemption in valid.values():
        assert exemption.justification.strip()
        assert exemption.owner.strip()
        assert exemption.review_by > TODAY
        assert exemption.invalidating_paths
