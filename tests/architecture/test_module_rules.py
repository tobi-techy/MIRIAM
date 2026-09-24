"""Architecture contract, enforced (docs/ARCHITECTURE-CONTRACT.md).

The contract used to promise an import-linter ini file and this AST suite;
neither existed, so every rule in it was prose. This module is the enforced
version. Two kinds of rule:

* **Import direction** — parsed from every module's AST, so a violation fails
  no matter how deep in a function the import hides. The edges encoded are
  the ones the layered design actually needs: money and its neighbours never
  reach back up to a turn layer, the rail never touches an LLM, integration
  clients never import the domain engine, and cross-cutting modules stay
  below everything.
* **Blob ratchet** — no file over 700 lines unless it is registered below
  with a reason, and a registered file may not grow past its recorded size.

Run with: pytest tests/architecture/ -q
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "miriam_agent"

# ---------------------------------------------------------------------------
# Import-direction rules
# ---------------------------------------------------------------------------

# package prefix -> packages it must never import
FORBIDDEN: dict[str, tuple[str, ...]] = {
    # Nothing below the orchestrator reaches back up to a turn/channel layer.
    "miriam_agent.hands": (
        "miriam_agent.api",
        "miriam_agent.agents",
        "miriam_agent.orchestrator",
        "miriam_agent.onboarding",
        "miriam_agent.proactive",
        "miriam_agent.voice",
    ),
    "miriam_agent.judgment": (
        "miriam_agent.api",
        "miriam_agent.orchestrator",
        "miriam_agent.onboarding",
        "miriam_agent.proactive",
    ),
    "miriam_agent.voice": (
        "miriam_agent.api",
        "miriam_agent.orchestrator",
        "miriam_agent.onboarding",
        "miriam_agent.proactive",
    ),
    "miriam_agent.money": (
        "miriam_agent.api",
        "miriam_agent.orchestrator",
        "miriam_agent.onboarding",
        "miriam_agent.proactive",
        "miriam_agent.agents",  # exception registered below
    ),
    # Integration clients are adapters: raw transport, no domain logic.
    "miriam_agent.integrations": (
        "miriam_agent.financial",
        "miriam_agent.hands",
        "miriam_agent.orchestrator",
    ),
    # Cross-cutting modules stay below the layers they serve.
    "miriam_agent.core": (
        "miriam_agent.financial",
        "miriam_agent.tools",
        "miriam_agent.database",
        "miriam_agent.integrations",
        "miriam_agent.hands",
        "miriam_agent.api",
        "miriam_agent.agents",
    ),
    "miriam_agent.config": (
        "miriam_agent.financial",
        "miriam_agent.tools",
        "miriam_agent.database",
        "miriam_agent.integrations",
        "miriam_agent.hands",
        "miriam_agent.api",
        "miriam_agent.agents",
    ),
    "miriam_agent.auth": (
        "miriam_agent.financial",
        "miriam_agent.tools",
        "miriam_agent.integrations",
        "miriam_agent.hands",
        "miriam_agent.api",
        "miriam_agent.agents",
    ),
    "miriam_agent.observability": (
        "miriam_agent.financial",
        "miriam_agent.tools",
        "miriam_agent.database",
        "miriam_agent.integrations",
        "miriam_agent.hands",
        "miriam_agent.api",
        "miriam_agent.agents",
    ),
    # Contract §4.1: the LLM layer never writes to storage directly.
    "miriam_agent.agents": ("miriam_agent.database",),
    # Safety gates judge turns; they do not drive them.
    "miriam_agent.safety": (
        "miriam_agent.orchestrator",
        "miriam_agent.agents",
    ),
}

# Registered exceptions: (source file, forbidden prefix, why it is tolerated).
# Every entry must carry the plan that removes it.
REGISTERED_EXCEPTIONS: dict[tuple[str, str], str] = {
    (
        "miriam_agent/money/agent.py",
        "miriam_agent.agents",
    ): (
        "Clamped narration for the deterministic money plan. It consumes the "
        "LLMProvider abstraction, which lives in agents.llm; the fix is to "
        "move the provider interface (ChatMessage/LLMProvider) into core so "
        "both voice and money can use it without reaching into the agent "
        "layer. Tracked with the voice/money provider split."
    ),
}


def _imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def _matches(module: str, banned: str) -> bool:
    return module == banned or module.startswith(banned + ".")


def test_import_direction():
    violations: list[str] = []
    for source in sorted(ROOT.rglob("*.py")):
        rel = str(source.relative_to(ROOT.parent)).replace("\\", "/")
        for owner, banned_prefixes in FORBIDDEN.items():
            owner_path = owner.replace(".", "/")
            if not (rel == owner_path or rel.startswith(owner_path + "/")):
                continue
            for module in _imports_of(source):
                for banned in banned_prefixes:
                    if not _matches(module, banned):
                        continue
                    if (rel, banned) in REGISTERED_EXCEPTIONS:
                        continue
                    violations.append(f"{rel}: imports {module} (forbidden)")
    assert not violations, "\n".join(violations)


def test_registered_exceptions_still_exist_and_are_needed():
    """A registered exception that no longer applies is removed, not rotting."""
    for (rel, banned), reason in REGISTERED_EXCEPTIONS.items():
        assert reason.strip(), f"{rel} -> {banned}: empty rationale"
        path = ROOT.parent / rel
        assert path.exists(), f"{rel}: registered but the file is gone"
        hits = [m for m in _imports_of(path) if _matches(m, banned)]
        assert hits, f"{rel}: registered for {banned} but no longer imports it"


# ---------------------------------------------------------------------------
# Blob ratchet (contract §4.3: no file over 700 LOC unless registered)
# ---------------------------------------------------------------------------

BLOB_LIMIT = 700

# path -> recorded size. A registered blob may not grow past its record; the
# ratchet only ever moves down, until the entry deletes itself.
REGISTERED_BLOBS: dict[str, int] = {
    "miriam_agent/financial/intelligence.py": 3280,
    "miriam_agent/onboarding/service.py": 1897,
    "miriam_agent/tools/definitions.py": 1387,
    "miriam_agent/financial/profile.py": 1376,
    "miriam_agent/orchestrator.py": 1050,
    # +12 for the empty-confirm_id 422 guards (false-settlement fix).
    "miriam_agent/api/chat.py": 1582,
    "miriam_agent/database/memory.py": 782,
    "miriam_agent/hands/transfer.py": 998,
    "miriam_agent/money/plan.py": 885,
    # +57 for the named funding-write methods (hands no longer calls private
    # _token_post; the validation/logging/retry contract lives in one place).
    "miriam_agent/integrations/go_client.py": 1266,
    "miriam_agent/integrations/supermemory_client.py": 829,
    "miriam_agent/safety/validator.py": 848,
    "miriam_agent/onboarding/driver.py": 747,
    "miriam_agent/financial/diagnosis.py": 715,
}


def _line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def test_blob_ratchet():
    problems: list[str] = []
    for source in sorted(ROOT.rglob("*.py")):
        rel = str(source.relative_to(ROOT.parent)).replace("\\", "/")
        size = _line_count(source)
        if rel in REGISTERED_BLOBS:
            if size > REGISTERED_BLOBS[rel]:
                problems.append(
                    f"{rel}: grew to {size} lines (registered at "
                    f"{REGISTERED_BLOBS[rel]}); the ratchet only moves down"
                )
            continue
        if size > BLOB_LIMIT:
            problems.append(
                f"{rel}: {size} lines exceeds the {BLOB_LIMIT}-line limit and "
                "is not registered in tests/architecture/test_module_rules.py"
            )
    assert not problems, "\n".join(problems)
