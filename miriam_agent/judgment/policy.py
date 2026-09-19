"""TypeSafe judgment thresholds.

Every magic number that drives a branch lives here, not scattered through the
gates. These are starting points to tune against real logs and the golden set;
changing a threshold is a product change and should be reviewed like one.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PolicyThresholds:
    # Ingress (PR1, active).
    intent_min_confidence: float = 0.62
    jailbreak_block: float = 0.80
    jailbreak_review: float = 0.55
    pii_block_reply: float = 0.75
    requests_disallowed_block: float = 0.75
    wants_human_escalate: float = 0.8
    needs_tools_planner: float = 0.6
    low_confidence_clarify: bool = True
    # Escalation is composed from two independent numbers: high frustration AND
    # a lower urgency bar (the standalone urgency threshold below is higher
    # because urgency alone is a weaker signal than urgency plus anger).
    frustration_escalate: float = 1.6
    escalation_urgency_min: float = 0.7
    urgency_escalate: float = 0.85
    frustration_soften_tone: float = 1.4
    # Tool gate. "Costly" (spends money) and "irreversible" (deletes data /
    # destroys / messages a third party) are separate questions because a single
    # blended question pushed ordinary transfers past the block bar.
    tool_risk_block: float = 0.85
    tool_risk_confirm: float = 0.55
    tool_costly_confirm: float = 0.55
    tool_relevance_reject_below: float = 0.5
    args_match_reject_below: float = 0.5
    exceeds_authority_block: float = 0.7
    # A user who explicitly confirmed this exact proposed action (name +
    # critical args) may pass a *confirm-band* action without a second prompt.
    # A hard irreversible/authority block is never overridden by confirmation.
    user_confirmed_allow: float = 0.8
    # Egress gate.
    egress_grounded_min: float = 0.70
    policy_violation_discard: float = 0.7
    leaks_system_discard: float = 0.6
    repeats_pii_discard: float = 0.6
    invents_facts_regenerate: float = 0.7
    # A draft that literally echoes a secret the user typed is discarded
    # outright (no regenerate -- the model already had the secret in context,
    # so a retry risks repeating it). Owns the literal-echo case explicitly so
    # it never depends on `repeats_pii` scoring high enough.
    echoes_user_secret_discard: float = 0.7


POLICY = PolicyThresholds()
