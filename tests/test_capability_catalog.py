"""Structural pins for the capability catalog and its deploy gate.

The catalog is only honest if its claims are checkable. These tests fail when
a claim points at a test that does not exist, when a proven capability has no
evidence, when a gap stops being written down, or when the trust bar is
quietly lowered.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from eval.capability_catalog import (  # noqa: E402
    PROVEN,
    Status,
    load_catalog,
    missing_evidence,
)
from eval.capability_policy import (  # noqa: E402
    QUALITY_PASS_RATE,
    TRUST_AREAS,
    TRUST_PASS_RATE,
    required_pass_rate,
)

CATALOG = load_catalog()

# Capabilities currently claimed as proven. Promoting or demoting one is a
# deliberate edit here, never a side effect of editing the JSON.
EXPECTED_PROVEN = {
    "C001",
    "C005",
    "C014",
    "C051",
    "C052",
    "C099",
    "C101",
    "C102",
    "C103",
    "C107",
    "C115",
    "C118",
}


def test_catalog_has_at_least_100_capabilities_with_unique_ids():
    ids = [c.id for c in CATALOG.capabilities]
    assert len(ids) >= 100
    assert len(ids) == len(set(ids))


def test_every_capability_carries_a_complete_eval():
    for cap in CATALOG.capabilities:
        spec = cap.eval
        assert all([spec.scenario, spec.expected, spec.pass_rule, spec.target]), cap.id


def test_every_evidence_node_resolves_to_a_real_test():
    assert missing_evidence(CATALOG) == []


def test_proven_capabilities_have_evidence_and_unproven_ones_have_none():
    for cap in CATALOG.capabilities:
        if cap.status in PROVEN:
            assert cap.evidence, f"{cap.id} is {cap.status.value} with no evidence"
        else:
            assert (
                not cap.evidence
            ), f"{cap.id} is {cap.status.value} but lists evidence"


def test_partial_and_unproven_capabilities_say_what_is_missing():
    for cap in CATALOG.capabilities:
        if cap.status in {Status.PARTIAL, Status.UNPROVEN}:
            assert cap.gap.strip(), f"{cap.id} must state its gap"


def test_the_proven_set_is_pinned():
    proven = {c.id for c in CATALOG.capabilities if c.status in PROVEN}
    assert proven == EXPECTED_PROVEN


def test_trust_areas_require_every_check_to_pass():
    assert TRUST_PASS_RATE == 1.0
    assert QUALITY_PASS_RATE >= 0.9
    for area in TRUST_AREAS:
        assert required_pass_rate(area) == 1.0


def test_trust_areas_exist_in_the_catalog():
    areas = {c.area for c in CATALOG.capabilities}
    assert TRUST_AREAS <= areas
