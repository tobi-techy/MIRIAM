"""Server-rendered allocate card JPEG (Spectrum Layer A).

The gateway never invents card copy: Miriam renders the JPEG from the same
fields the card part carries, and the gateway only attaches the bytes.
1080px wide, dark, Flip-beat visual language: ALLOCATE, symbols, amount,
source pot.
"""

from __future__ import annotations

import base64
import io

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt

WIDTH_PX = 1080
DPI = 180

BG = "#101014"
TEXT = "#f2f2f5"
MUTED = "#9a9aa5"
ACCENT = "#4da3ff"
LINE = "#2a2a33"


def allocate_jpg(
    *,
    title: str = "ALLOCATE",
    subtitle: str = "Glider \u00b7 Rail Stock Sleeve",
    primary: str = "AAPLx 40 / NVDAx 30 / TSLAx 30",
    amount: str = "",
    source: str = "stash",
    cta: str = "Approve",
) -> bytes:
    fig, ax = plt.subplots(figsize=(WIDTH_PX / DPI, WIDTH_PX / DPI * 0.52), dpi=DPI)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.set_axis_off()
    ax.plot([6, 94], [78, 78], color=LINE, linewidth=1.5)
    ax.text(
        6, 90, title, color=TEXT, fontsize=34, fontweight="bold", va="center", ha="left"
    )
    ax.text(6, 72, subtitle, color=MUTED, fontsize=19, va="center", ha="left")
    ax.text(6, 52, primary, color=TEXT, fontsize=24, va="center", ha="left")
    ax.text(
        6,
        32,
        amount,
        color=ACCENT,
        fontsize=30,
        fontweight="bold",
        va="center",
        ha="left",
    )
    ax.text(6, 18, f"from {source}", color=MUTED, fontsize=18, va="center", ha="left")
    ax.text(
        94,
        18,
        cta.upper(),
        color=BG,
        fontsize=20,
        fontweight="bold",
        va="center",
        ha="right",
        bbox={"boxstyle": "round,pad=0.6", "facecolor": ACCENT, "edgecolor": ACCENT},
    )
    buf = io.BytesIO()
    fig.savefig(buf, format="jpg", facecolor=BG, bbox_inches="tight", pad_inches=0.2)
    plt.close(fig)
    return buf.getvalue()


def jpg_base64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


__all__ = ["allocate_jpg", "jpg_base64"]
