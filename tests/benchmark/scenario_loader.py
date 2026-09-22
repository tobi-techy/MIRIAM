"""Scenario loader and registry for Miriam benchmarks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.benchmark.models import (
    BenchmarkScenario,
    ConfirmationRequired,
    ConversationTurn,
    EvaluationRubric,
    ExpectedToolCall,
    FinancialState,
    ForbiddenBehavior,
    ScenarioCategory,
    UserProfile,
)


def load_scenarios_from_dir(scenarios_dir: str | Path) -> list[BenchmarkScenario]:
    """Load all scenarios from JSON files in a directory."""
    scenarios = []
    dir_path = Path(scenarios_dir)
    for json_file in sorted(dir_path.glob("*.json")):
        with open(json_file) as f:
            data = json.load(f)
        scenario = _dict_to_scenario(data)
        scenarios.append(scenario)
    return scenarios


def _dict_to_scenario(data: dict[str, Any]) -> BenchmarkScenario:
    """Convert a dictionary to a BenchmarkScenario."""
    user_profile = UserProfile(**data.get("user_profile", {}))
    financial_state = FinancialState(**data.get("financial_state", {}))

    conversation_history = [
        ConversationTurn(**turn) for turn in data.get("conversation_history", [])
    ]

    required_tool_calls = [
        ExpectedToolCall(**tc) for tc in data.get("required_tool_calls", [])
    ]

    forbidden_behaviors = [
        ForbiddenBehavior(**fb) for fb in data.get("forbidden_behaviors", [])
    ]

    rubric = EvaluationRubric(**data.get("rubric", {}))

    return BenchmarkScenario(
        id=data["id"],
        name=data["name"],
        category=ScenarioCategory(data["category"]),
        description=data["description"],
        user_profile=user_profile,
        financial_state=financial_state,
        conversation_history=conversation_history,
        user_request=data["user_request"],
        expected_intent=data["expected_intent"],
        expected_reasoning_path=data["expected_reasoning_path"],
        allowed_tools=data.get("allowed_tools", []),
        required_tool_calls=required_tool_calls,
        confirmation_required=ConfirmationRequired(
            data.get("confirmation_required", "not_required")
        ),
        expected_final_outcome=data.get("expected_final_outcome", ""),
        forbidden_behaviors=forbidden_behaviors,
        rubric=rubric,
        tags=data.get("tags", []),
        difficulty=data.get("difficulty", "medium"),
        version=data.get("version", "1.0"),
    )


def save_scenario(scenario: BenchmarkScenario, scenarios_dir: str | Path) -> Path:
    """Save a scenario to a JSON file."""
    dir_path = Path(scenarios_dir)
    dir_path.mkdir(parents=True, exist_ok=True)

    data = {
        "id": scenario.id,
        "name": scenario.name,
        "category": scenario.category.value,
        "description": scenario.description,
        "user_profile": {
            "user_id": scenario.user_profile.user_id,
            "name": scenario.user_profile.name,
            "location": scenario.user_profile.location,
            "currency": scenario.user_profile.currency,
            "risk_tolerance": scenario.user_profile.risk_tolerance,
            "financial_goals": scenario.user_profile.financial_goals,
            "known_facts": scenario.user_profile.known_facts,
        },
        "financial_state": {
            "balances": scenario.financial_state.balances,
            "income": scenario.financial_state.income,
            "expenses": scenario.financial_state.expenses,
            "debts": scenario.financial_state.debts,
            "investments": scenario.financial_state.investments,
            "obligations": scenario.financial_state.obligations,
            "missing_data": scenario.financial_state.missing_data,
            "last_statement_date": scenario.financial_state.last_statement_date,
        },
        "conversation_history": [
            {
                "role": turn.role,
                "content": turn.content,
                "intent": turn.intent,
                "tool_calls": turn.tool_calls,
                "timestamp": turn.timestamp,
            }
            for turn in scenario.conversation_history
        ],
        "user_request": scenario.user_request,
        "expected_intent": scenario.expected_intent,
        "expected_reasoning_path": scenario.expected_reasoning_path,
        "allowed_tools": scenario.allowed_tools,
        "required_tool_calls": [
            {
                "tool_name": tc.tool_name,
                "arguments": tc.arguments,
                "tool_type": tc.tool_type.value,
                "order": tc.order,
                "is_required": tc.is_required,
            }
            for tc in scenario.required_tool_calls
        ],
        "confirmation_required": scenario.confirmation_required.value,
        "expected_final_outcome": scenario.expected_final_outcome,
        "forbidden_behaviors": [
            {
                "description": fb.description,
                "detection_pattern": fb.detection_pattern,
                "severity": fb.severity,
            }
            for fb in scenario.forbidden_behaviors
        ],
        "rubric": {
            "intent_accuracy": scenario.rubric.intent_accuracy,
            "reasoning_quality": scenario.rubric.reasoning_quality,
            "tool_selection": scenario.rubric.tool_selection,
            "tool_arguments": scenario.rubric.tool_arguments,
            "safety_compliance": scenario.rubric.safety_compliance,
            "outcome_correctness": scenario.rubric.outcome_correctness,
            "communication_quality": scenario.rubric.communication_quality,
            "critical_failures": scenario.rubric.critical_failures,
        },
        "tags": scenario.tags,
        "difficulty": scenario.difficulty,
        "version": scenario.version,
    }

    file_path = dir_path / f"{scenario.id}.json"
    with open(file_path, "w") as f:
        json.dump(data, f, indent=2)

    return file_path


# Registry for programmatic access
_SCENARIO_REGISTRY: dict[str, BenchmarkScenario] = {}


def register_scenario(scenario: BenchmarkScenario) -> None:
    """Register a scenario in the global registry."""
    _SCENARIO_REGISTRY[scenario.id] = scenario


def get_scenario(scenario_id: str) -> BenchmarkScenario | None:
    """Get a scenario by ID."""
    return _SCENARIO_REGISTRY.get(scenario_id)


def get_all_scenarios() -> list[BenchmarkScenario]:
    """Get all registered scenarios."""
    return list(_SCENARIO_REGISTRY.values())


def get_scenarios_by_category(category: ScenarioCategory) -> list[BenchmarkScenario]:
    """Get all scenarios in a category."""
    return [s for s in _SCENARIO_REGISTRY.values() if s.category == category]
