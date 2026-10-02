"""Shadow-run parity: prove the trust boundary agrees with production turns.

Reads recorded turns (JSONL, one ``RecordedTurn`` per line), re-runs the trust
boundary dry via ``hands/shadow.py``, and reports divergence. Read-only: no
rail, no ledger writes, no shared journal claims.

Exit 0 when clean, 1 on any divergence (CI-gateable)::

    uv run python scripts/shadow_parity.py tests/fixtures/shadow/corpus.jsonl
    uv run python scripts/shadow_parity.py --policy-max-auto 100 turns.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from miriam_agent.hands.ledger import money  # noqa: E402
from miriam_agent.hands.limits import Policy  # noqa: E402
from miriam_agent.hands.shadow import run_corpus, turn_from_dict  # noqa: E402


def load_corpus(path: Path) -> list:
    turns = []
    for lineno, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            turns.append(turn_from_dict(json.loads(line)))
        except Exception as exc:  # noqa: BLE001 - bad line fails the run loudly
            print(f"{path}:{lineno}: unparseable line: {exc}", file=sys.stderr)
            sys.exit(2)
    return turns


async def _main(args: argparse.Namespace) -> int:
    turns = load_corpus(Path(args.corpus))
    if not turns:
        print("no turns in corpus", file=sys.stderr)
        return 2
    policy = Policy(
        max_auto=money(args.policy_max_auto),
        max_with_confirm=money(args.policy_max_confirm),
        reversible_under=money(args.policy_max_auto),
        max_daily=money(args.policy_max_daily),
    )
    report = await run_corpus(turns, policy=policy)
    print(f"shadow parity: {report.total} turns, {len(report.diverged)} diverged")
    for verdict in report.diverged:
        print(f"  DIVERGED {verdict.turn_id}: {verdict.divergence_note}")
    return 1 if not report.clean else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", help="JSONL recorded-turn corpus")
    parser.add_argument("--policy-max-auto", default="2000")
    parser.add_argument("--policy-max-confirm", default="100000")
    parser.add_argument("--policy-max-daily", default="10000")
    args = parser.parse_args()
    sys.exit(asyncio.run(_main(args)))


if __name__ == "__main__":
    main()
