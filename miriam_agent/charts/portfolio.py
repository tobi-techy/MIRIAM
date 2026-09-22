"""Server-side portfolio charts for the Spectrum surface.

iMessage cannot render Recharts HTML, so Miriam renders PNG here and the
gateway sends it as an attachment. Every figure is 1080px wide, dark, and
readable on a phone.

The one hard rule: data comes only from ``glider_get_positions`` and the Rail
ledger. An empty position list returns ``None`` (the caller sends
"nothing indexed yet" as text) -- a chart is never invented.
"""

from __future__ import annotations

import base64
import io
from decimal import Decimal
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.patches import Wedge

WIDTH_PX = 1080
DPI = 180
WIDTH_IN = WIDTH_PX / DPI

BG = "#101014"
CARD = "#17171d"
TEXT = "#f2f2f5"
MUTED = "#9a9aa5"
ACCENT = ["#4da3ff", "#ff9f43", "#3ddc84", "#c26bff", "#ff6b81", "#ffd166"]
GRID = "#2a2a33"


def _fig(title: str, subtitle: str = "") -> tuple[Any, Any]:
    fig, ax = plt.subplots(figsize=(WIDTH_IN, WIDTH_IN * 0.62), dpi=DPI)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    ax.text(
        0.06,
        0.94,
        title,
        transform=ax.transAxes,
        color=TEXT,
        fontsize=30,
        fontweight="bold",
        va="top",
        ha="left",
    )
    if subtitle:
        ax.text(
            0.06,
            0.865,
            subtitle,
            transform=ax.transAxes,
            color=MUTED,
            fontsize=19,
            va="top",
            ha="left",
        )
    return fig, ax


def _png(fig: Any) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG, bbox_inches="tight", pad_inches=0.25)
    plt.close(fig)
    return buf.getvalue()


def _positions(positions: list[dict[str, Any]]) -> list[tuple[str, float]]:
    rows: list[tuple[str, float]] = []
    for p in positions:
        symbol = str(p.get("symbol") or p.get("asset") or "?")
        try:
            value = float(Decimal(str(p.get("value_usd") or p.get("valueUsd") or 0)))
        except (ArithmeticError, TypeError, ValueError):
            continue
        if value > 0:
            rows.append((symbol, value))
    rows.sort(key=lambda r: -r[1])
    return rows


def sleeve_donut(
    positions: list[dict[str, Any]], title: str = "Rail Stock Sleeve"
) -> bytes | None:
    """Donut of the sleeve from live positions. None when nothing indexed."""
    rows = _positions(positions)
    if not rows:
        return None
    total = sum(v for _, v in rows)
    fig, ax = _fig(title, f"${total:,.2f} indexed")
    ax.axis("equal")
    ax.set_xlim(-1.4, 1.4)
    wedges = ax.pie(
        [v for _, v in rows],
        colors=(ACCENT * ((len(rows) // len(ACCENT)) + 1))[: len(rows)],
        startangle=90,
        counterclock=False,
        wedgeprops={"width": 0.42, "edgecolor": BG, "linewidth": 3},
    )[0]
    assert isinstance(wedges, list)
    for i, ((symbol, value), wedge) in enumerate(zip(rows, wedges)):
        assert isinstance(wedge, Wedge)
        pct = value / total * 100
        angle = (wedge.theta2 + wedge.theta1) / 2
        ax.text(
            1.25 * __import__("math").cos(__import__("math").radians(angle)),
            1.25 * __import__("math").sin(__import__("math").radians(angle)),
            f"{symbol}\n${value:,.0f} ({pct:.0f}%)",
            color=TEXT,
            fontsize=17,
            ha="center",
            va="center",
        )
    ax.text(
        0,
        0.08,
        f"${total:,.0f}",
        color=TEXT,
        fontsize=34,
        fontweight="bold",
        ha="center",
        va="center",
    )
    ax.text(0, -0.14, "invested", color=MUTED, fontsize=18, ha="center", va="center")
    ax.set_axis_off()
    return _png(fig)


def before_after_bar(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    title: str = "Deposit indexed",
) -> bytes | None:
    """Before/after bars per symbol. None when the after side is empty."""
    b = dict(_positions(before))
    a = dict(_positions(after))
    if not a:
        return None
    symbols = sorted(set(b) | set(a))
    x = range(len(symbols))
    bv = [b.get(s, 0.0) for s in symbols]
    av = [a.get(s, 0.0) for s in symbols]
    fig, ax = _fig(title, "per-symbol value, USD")
    w = 0.36
    ax.bar([i - w / 2 for i in x], bv, width=w, color=MUTED, label="before")
    ax.bar([i + w / 2 for i in x], av, width=w, color=ACCENT[0], label="after")
    ax.set_xticks(list(x))
    ax.set_xticklabels(symbols, color=TEXT, fontsize=18)
    ax.tick_params(colors=MUTED, labelsize=16)
    for spine in ax.spines.values():
        spine.set_color(GRID)
    ax.grid(axis="y", color=GRID, linewidth=0.6, alpha=0.7)
    leg = ax.legend(frameon=False, fontsize=16)
    for t in leg.get_texts():
        t.set_color(TEXT)
    return _png(fig)


def split_bar(
    spend: float, invest: float, title: str = "Latest inbound 70/30"
) -> bytes | None:
    """Spend-vs-invest proof bar for the latest inbound. None on zeros."""
    if spend <= 0 and invest <= 0:
        return None
    total = spend + invest
    fig, ax = _fig(title, f"${total:,.2f} in")
    ax.barh(
        ["spend", "invest"], [spend, invest], color=[ACCENT[1], ACCENT[2]], height=0.45
    )
    ax.set_xlim(0, max(total * 1.25, 1))
    for i, v in enumerate([spend, invest]):
        pct = v / total * 100 if total else 0
        ax.text(
            v + total * 0.02,
            i,
            f"${v:,.0f} ({pct:.0f}%)",
            color=TEXT,
            fontsize=22,
            va="center",
        )
    ax.tick_params(colors=TEXT, labelsize=20)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.set_yticks([0, 1])
    ax.set_yticklabels(["spend", "invest"], color=TEXT, fontsize=22)
    ax.grid(False)
    return _png(fig)


def png_base64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


__all__ = [
    "before_after_bar",
    "png_base64",
    "sleeve_donut",
    "split_bar",
]
