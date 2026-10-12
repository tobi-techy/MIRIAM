"""Live eval for the shop document reader.

Runs every fixture through the real vision provider on llmapi.ai, in three
modes so the routing decision can be judged on evidence:

``small``   only ``READ_SMALL_MODEL``, no escalation
``big``     only ``READ_BIG_MODEL``, no escalation
``routed``  the real Jev router, real escalation, exactly what production does

Needs ``LLMAPI_API_KEY``; ``routed`` also needs ``TYPESAFE_API_KEY``. Without
them the run is skipped with a clear message rather than failing.

Run::

    uv run python -m eval.shop_read_eval                 # every mode
    uv run python -m eval.shop_read_eval --modes routed
    uv run python -m eval.shop_read_eval --case wigmore_invoice_pdf

Every figure the reader prints is checked here against the number a human read
off the same page. A miss is printed with the expected and the observed value,
never smoothed over.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURES = ROOT / "tests" / "fixtures"

MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".pdf": "application/pdf",
}


@dataclass(frozen=True)
class Case:
    name: str
    filename: str
    doc_kind: str
    expect: dict[str, Any]
    note: str = ""


# Stock book rows in mama_tobi add up; only the written total is short, which is
# what the amendment says this page shows: N254,400 written against N286,300 of
# sales from the prices.
CASES: list[Case] = [
    Case(
        name="mama_tobi_stock_book",
        filename="stock_book_mama_tobi.png",
        doc_kind="stock_book",
        expect={
            "doc_type": "stock_book",
            "computed_total": "N286,300",
            "written_total": "N254,400",
            "gap": "N31,900",
            "rows_failing": 0,
        },
    ),
    Case(
        name="ledger_v2_stock_book",
        filename="stock_book_ledger_v2.png",
        doc_kind="stock_book",
        expect={
            "doc_type": "stock_book",
            "computed_total": "N248,000",
            "written_total": "N236,000",
            "gap": "N12,000",
            "rows_failing": 0,
        },
    ),
    Case(
        name="ledger_v2_scanned_pdf",
        filename="stock_book_ledger_v2_scan.pdf",
        doc_kind="stock_book",
        expect={
            "doc_type": "stock_book",
            "computed_total": "N248,000",
            "written_total": "N236,000",
            "gap": "N12,000",
        },
        note="scanned PDF: rasterised, native text is not usable",
    ),
    Case(
        name="wigmore_invoice_pdf",
        filename="wigmore_invoice_WGT-2610-0417.pdf",
        doc_kind="invoice",
        expect={
            "doc_type": "invoice",
            "lines_read": 5,
            "computed_subtotal": "N180,600",
            "printed_subtotal": "N182,600",
            "subtotal_delta": "N2,000",
            "line_mismatches": 1,
            "line_mismatch_1": (
                "Milo 400g refill: 6 x N3,800 = N22,800, printed N24,800, "
                "mismatch N2,000"
            ),
            "discount": "N2,600",
            "total": "N180,000",
            "paid": "N150,000",
            "balance": "N30,000",
            "due": "17/10/2026",
        },
        note="text PDF: native text is usable, image still sent",
    ),
    Case(
        name="wigmore_invoice_photo",
        filename="wigmore_invoice_phone_photo.jpg",
        doc_kind="invoice",
        expect={
            "doc_type": "invoice",
            "lines_read": 5,
            "computed_subtotal": "N180,600",
            "printed_subtotal": "N182,600",
            "subtotal_delta": "N2,000",
            "line_mismatches": 1,
            "total": "N180,000",
            "balance": "N30,000",
        },
        note="phone photo of the same invoice",
    ),
]


@dataclass
class Observation:
    ok: bool
    values: dict[str, Any] = field(default_factory=dict)
    misses: list[str] = field(default_factory=list)
    error: str = ""
    latency_s: float = 0.0
    cost_usd: float = 0.0
    calls: int = 0
    doc_type: str = ""
    tier_path: str = ""


class _ForcedJudge:
    """A judge that forces one read shape, for the small-only/big-only modes."""

    def __init__(self, *, difficulty: str, doc_kind: str) -> None:
        self.difficulty = difficulty
        self.doc_kind = doc_kind

    async def choose_tier(self, state: Any) -> Any:
        from typesafe_sdk import ChoiceAnswer

        from miriam_agent.judgment.reading import TierChoiceJudgment

        def _choice(value: str) -> ChoiceAnswer:
            return ChoiceAnswer.model_construct(
                type="choice",
                choice=value,
                confidence=0.99,
                probabilities={value: 0.99},
            )

        return TierChoiceJudgment.model_construct(
            model="eval",
            usage={},
            answers=[],
            doc_kind=_choice(self.doc_kind),
            difficulty=_choice(self.difficulty),
        )

    async def check_quality(self, state: Any) -> Any:
        from typesafe_sdk import NoulAnswer, ScoreAnswer

        from miriam_agent.judgment.reading import ExtractionQualityJudgment

        return ExtractionQualityJudgment.model_construct(
            model="eval",
            usage={},
            answers=[],
            extraction_quality=ScoreAnswer.model_construct(
                type="score",
                score=5,
                confidence=0.99,
                legend={},
                probabilities={5: 0.99},
            ),
            misalignment_likely=NoulAnswer.model_construct(type="noul", noul=0.0),
        )

    async def classify_note(self, state: Any) -> Any:
        from typesafe_sdk import ChoiceAnswer

        from miriam_agent.judgment.reading import NoteKindJudgment

        return NoteKindJudgment.model_construct(
            model="eval",
            usage={},
            answers=[],
            kind=ChoiceAnswer.model_construct(
                type="choice",
                choice="other",
                confidence=0.99,
                probabilities={"other": 0.99},
            ),
        )


class _RecordingProvider:
    """Wraps the real llmapi provider and totals cost, tokens and latency."""

    def __init__(self, model: str) -> None:
        from miriam_agent.agents.llm import LLMAPIProvider

        self.model = model
        self.inner = LLMAPIProvider(model)
        self.cost_usd = 0.0
        self.latency_s = 0.0
        self.calls = 0
        self.tokens = 0

    async def complete_structured(
        self,
        messages: list[Any],
        schema_model: Any,
        *,
        reasoning_effort: str | None = None,
        max_tokens: int | None = None,
    ) -> Any:
        response = await self.inner.complete_structured(
            messages,
            schema_model,
            reasoning_effort=reasoning_effort,
            max_tokens=max_tokens,
        )
        self.calls += 1
        self.cost_usd += float(response.usage.get("cost_usd", 0.0))
        self.latency_s += float(response.usage.get("latency_ms", 0.0)) / 1000.0
        self.tokens += int(response.usage.get("total_tokens", 0))
        return response

    async def aclose(self) -> None:
        await self.inner.client.close()


# Every recorder built during a run, so the run can close its HTTP clients.
_OPEN_PROVIDERS: list[_RecordingProvider] = []


class _CapturingStore:
    """The in-memory store, plus the raw extraction the reader saved.

    The extraction is what a human compares against the page, and the store is
    the only place the reader hands it over, so the eval reads it there rather
    than adding a response field the Go client would have to know about.
    """

    def __init__(self) -> None:
        from miriam_agent.shop.store import InMemoryShopDocumentStore

        self.inner = InMemoryShopDocumentStore()
        self.saved: dict[str, Any] = {}

    async def find_by_sha(self, user_id: str, sha256: str) -> Any:
        return await self.inner.find_by_sha(user_id, sha256)

    async def save(self, **kwargs: Any) -> Any:
        self.saved = dict(kwargs)
        return await self.inner.save(**kwargs)


def _naira_or_none(value: str | None) -> str | None:
    from miriam_agent.hands.payment_refs import amount_to_minor
    from miriam_agent.shop.checks import naira

    if value is None:
        return None
    return naira(amount_to_minor(value))


def _observe(result: Any, saved: dict[str, Any]) -> dict[str, Any]:
    """The figures a human can compare off the page."""
    from miriam_agent.shop.checks import (
        check_stock_book,
        check_supplier_document,
    )
    from miriam_agent.shop.schemas import StockBook, SupplierDocument

    values: dict[str, Any] = {"doc_type": result.doc_type}
    raw = saved.get("raw_extraction")
    if not raw:
        return values
    if saved.get("doc_type") == "stock_book":
        extraction: Any = StockBook.model_validate(raw)
    else:
        extraction = SupplierDocument.model_validate(raw)

    if isinstance(extraction, StockBook):
        report = check_stock_book(extraction)
        values.update(
            {
                "rows_read": len(extraction.rows),
                "computed_total": _naira_or_none(
                    str(report.computed_total_minor // 100)
                ),
                "written_total": _naira_or_none(extraction.written_total_sales_naira),
                "gap": _naira_or_none(str(report.gap_minor // 100)),
                "rows_failing": report.rows_failing,
            }
        )
    elif isinstance(extraction, SupplierDocument):
        report = check_supplier_document(extraction)
        values.update(
            {
                "lines_read": len(extraction.lines),
                "computed_subtotal": _naira_or_none(
                    str(report.computed_subtotal_minor // 100)
                ),
                "printed_subtotal": (
                    _naira_or_none(str(report.printed_subtotal_minor // 100))
                    if report.printed_subtotal_minor is not None
                    else None
                ),
                "subtotal_delta": _naira_or_none(
                    str(report.subtotal_delta_minor // 100)
                ),
                "line_mismatches": len(report.line_mismatches),
                "line_mismatch_1": (
                    report.line_mismatches[0] if report.line_mismatches else ""
                ),
                "discount": _naira_or_none(extraction.discount_naira),
                "total": _naira_or_none(extraction.total_naira),
                "paid": _naira_or_none(extraction.paid_naira),
                "balance": _naira_or_none(extraction.balance_naira),
                "due": extraction.due_date.strip(),
            }
        )
    return values


async def _run_case(case: Case, mode: str) -> Observation:
    from miriam_agent.config.settings import get_settings
    from miriam_agent.shop.reader import ReadFailed
    from miriam_agent.shop.service import read_shop_document

    settings = get_settings()
    path = FIXTURES / case.filename
    data = path.read_bytes()
    mime = MIME_BY_SUFFIX[path.suffix.lower()]

    providers: dict[str, _RecordingProvider] = {}

    def factory(tier: str) -> _RecordingProvider:
        if tier not in providers:
            model = (
                settings.READ_SMALL_MODEL
                if tier == "small"
                else settings.READ_BIG_MODEL
            )
            providers[tier] = _RecordingProvider(model)
            _OPEN_PROVIDERS.append(providers[tier])
        return providers[tier]

    if mode == "small":
        judge: Any = _ForcedJudge(difficulty="easy", doc_kind=case.doc_kind)
        router_enabled = True
    elif mode == "big":
        judge = _ForcedJudge(difficulty="hard", doc_kind=case.doc_kind)
        router_enabled = True
    else:
        from miriam_agent.judgment.reading import ReadingJudge

        judge = ReadingJudge()
        router_enabled = None

    start = time.perf_counter()
    error = ""
    result = None
    store = _CapturingStore()
    try:
        result = await asyncio.wait_for(
            read_shop_document(
                file_b64=base64.b64encode(data).decode(),
                mime=mime,
                user_id="eval-user",
                message_id=f"eval-{case.name}-{mode}",
                doc_hint=case.doc_kind,
                store=store,
                judge=judge,
                provider_factory=factory,
                router_enabled=router_enabled,
            ),
            timeout=settings.SHOP_READ_WALL_CLOCK_S,
        )
    except ReadFailed as exc:
        error = f"failed closed: {exc}"
    except TimeoutError:
        error = f"timed out after {settings.SHOP_READ_WALL_CLOCK_S:.0f}s"
    except Exception as exc:  # noqa: BLE001 - the eval reports, never hides
        error = f"{type(exc).__name__}: {exc}"
    latency_s = time.perf_counter() - start

    cost = sum(p.cost_usd for p in providers.values())
    calls = sum(p.calls for p in providers.values())

    if result is None:
        return Observation(
            ok=False, error=error, latency_s=latency_s, cost_usd=cost, calls=calls
        )

    values = _observe(result, store.saved)
    misses = [
        f"{key}: expected {expected!r}, got {values.get(key)!r}"
        for key, expected in case.expect.items()
        if values.get(key) != expected
    ]
    if error:
        misses.append(error)
    return Observation(
        ok=not misses,
        values=values,
        misses=misses,
        latency_s=latency_s,
        cost_usd=cost,
        calls=calls,
        doc_type=result.doc_type,
        tier_path="+".join(result.model_path),
    )


def _table(rows: list[tuple[Case, str, Observation]]) -> str:
    if not rows:
        return ""
    out = [
        "| fixture | mode | kind | read | figures | latency | cost | tier path |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for case, mode, observation in rows:
        figures = "match" if observation.ok else f"MISS ({len(observation.misses)})"
        out.append(
            "| {name} | {mode} | {kind} | {read} | {figures} | {latency} | "
            "{cost} | {path} |".format(
                name=case.name,
                mode=mode,
                kind=observation.doc_type or case.doc_kind,
                read="yes" if observation.values else "no",
                figures=figures,
                latency=f"{observation.latency_s:.1f}s",
                cost=f"${observation.cost_usd:.4f}",
                path=observation.tier_path or "-",
            )
        )
    return "\n".join(out)


def _summary(rows: list[tuple[Case, str, Observation]], modes: list[str]) -> str:
    out: list[str] = []
    for mode in modes:
        subset = [o for _, m, o in rows if m == mode]
        if not subset:
            continue
        hits = sum(1 for o in subset if o.ok)
        total = len(subset)
        cost = sum(o.cost_usd for o in subset)
        latencies = sorted(o.latency_s for o in subset)
        median = latencies[len(latencies) // 2]
        out.append(
            f"- {mode}: {hits}/{total} exact, mean ${cost / total:.4f} per read, "
            f"median {median:.1f}s"
        )
    return "\n".join(out)


def _misses(rows: list[tuple[Case, str, Observation]]) -> str:
    lines: list[str] = []
    for case, mode, observation in rows:
        if observation.ok:
            continue
        lines.append(f"\n{case.name} [{mode}]")
        if observation.error and not observation.values:
            lines.append(f"  {observation.error}")
        for miss in observation.misses:
            lines.append(f"  {miss}")
        if observation.values:
            lines.append(f"  observed: {observation.values}")
    return "\n".join(lines)


async def _close_everything() -> None:
    from miriam_agent.judgment.reading import close_reader_client

    for provider in _OPEN_PROVIDERS:
        try:
            await provider.aclose()
        except Exception:  # noqa: BLE001 - shutdown is best effort
            pass
    _OPEN_PROVIDERS.clear()
    await close_reader_client()


async def run(modes: list[str], names: list[str] | None) -> int:
    from miriam_agent.config.settings import get_settings

    settings = get_settings()
    if not settings.LLMAPI_API_KEY:
        print("SKIPPED: LLMAPI_API_KEY is not set, so no live read can run.")
        return 0
    if "routed" in modes and not settings.TYPESAFE_API_KEY:
        print("SKIPPED: TYPESAFE_API_KEY is not set, so routed mode cannot run.")
        return 0

    cases = [c for c in CASES if not names or c.name in names]
    missing = [c for c in cases if not (FIXTURES / c.filename).exists()]
    cases = [c for c in cases if (FIXTURES / c.filename).exists()]

    for case in missing:
        print(f"SKIPPED (fixture missing): {case.name} -> {case.filename}")
    if not cases:
        print("No fixtures to run.")
        return 1

    print(
        f"models: {settings.READ_SMALL_MODEL} (small) / "
        f"{settings.READ_BIG_MODEL} (big)"
    )
    print(f"cases: {', '.join(c.name for c in cases)}")
    print(f"modes: {', '.join(modes)}")

    rows: list[tuple[Case, str, Observation]] = []
    spent = 0.0
    for case in cases:
        for mode in modes:
            observation = await _run_case(case, mode)
            spent += observation.cost_usd
            rows.append((case, mode, observation))
            print(
                f"  {case.name:26s} {mode:7s} "
                f"{'ok' if observation.ok else 'MISS':4s} "
                f"{observation.latency_s:5.1f}s ${observation.cost_usd:.4f}"
            )
            if spent > settings.SHOP_READ_MAX_COST_USD:
                print(
                    f"STOPPED: eval spend ${spent:.4f} exceeded "
                    f"SHOP_READ_MAX_COST_USD ${settings.SHOP_READ_MAX_COST_USD}"
                )
                await _close_everything()
                return 1

    print("\n## Live eval\n")
    print(_table(rows))
    print("\n### Summary\n")
    print(_summary(rows, modes))
    print(f"\nTotal spend: ${spent:.4f}")
    misses = _misses(rows)
    if misses:
        print("\n### Misses\n")
        print(misses)

    await _close_everything()
    return 0 if all(o.ok for _, _, o in rows) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--modes",
        default="small,big,routed",
        help="comma-separated subset of small,big,routed",
    )
    parser.add_argument("--case", action="append", help="case name (repeatable)")
    args = parser.parse_args()
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    unknown = [m for m in modes if m not in {"small", "big", "routed"}]
    if unknown:
        parser.error(f"unknown mode(s): {', '.join(unknown)}")
    os.environ.setdefault("SHOP_DOC_STORE", "memory")
    return asyncio.run(run(modes, args.case))


if __name__ == "__main__":
    sys.exit(main())
