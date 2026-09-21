"""Inline SVG figures for the printed plan (Stage 02 PRD §14).

§14 asks the PDF for "a media plan with forecast charts" and "a channel slate
timeline". Three figures satisfy that, and all three are drawn with
`export/charts.py`'s primitives rather than a plotting library — for the
reason that module argues at length: matplotlib's SVG embeds font metrics and
glyph paths, so the same plan rendered on two machines produces different
bytes, and §14's acceptance 6 is "exporting a frozen plan twice produces
byte-identical PDFs".

Nothing here computes a finding. An axis is scaled and a label is truncated,
and that is the whole extent of the judgement.
"""

from __future__ import annotations

from collections.abc import Sequence

from agent.export.charts import ACCENT, GRID, INK, MUTED, _escape, _round, bar_chart
from agent.export.plan_contract import CampaignPlan

#: Bars in the allocation figure. Past this a horizontal bar chart is a wall,
#: and the table above it is the readable rendering anyway.
MAX_BARS = 14

#: Characters of a campaign name on an axis. A 60-character name pushes the
#: plot area to nothing.
LABEL_CHARS = 26


def build_plan_charts(plan: CampaignPlan) -> dict[str, str]:
    """Every figure the print template may place, keyed by its slot."""
    return {
        "allocation": allocation_chart(plan),
        "forecast": forecast_chart(plan),
        "waves": wave_chart(plan),
    }


def allocation_chart(plan: CampaignPlan) -> str:
    """Monthly spend by campaign, largest first."""
    lines = sorted(plan.media_plan.allocation, key=lambda row: -row.usd)[:MAX_BARS]
    if not lines:
        return ""
    return bar_chart(
        [_label(row.campaign_ref) for row in lines],
        [row.usd for row in lines],
        value_labels=[f"{row.usd:,.0f}" for row in lines],
    )


def forecast_chart(plan: CampaignPlan) -> str:
    """Conversions and cost by month — the two series a budget owner compares.

    Cost is drawn as the bar and conversions as a point on its own scale,
    because they share an axis in no meaningful unit. The point is what makes
    a month where cost rises and conversions do not visible at a glance, which
    is the only thing this figure is for.
    """
    months: dict[str, tuple[float, float]] = {}
    for row in plan.media_plan.forecast:
        month = (row.month or "").strip() or "—"
        cost, conversions = months.get(month, (0.0, 0.0))
        months[month] = (cost + (row.cost_usd or 0.0), conversions + (row.conversions or 0.0))
    if len(months) < 2:
        return ""

    labels = list(months)
    costs = [months[month][0] for month in labels]
    # Not `conversions` — that name is the loop variable above, and rebinding it
    # to a list is the kind of shadowing mypy catches and a reader does not.
    counts = [months[month][1] for month in labels]
    return _paired_columns(labels, costs, counts)


def wave_chart(plan: CampaignPlan) -> str:
    """The launch timeline §14 asks for: one row per wave, channels inside it."""
    waves: dict[int, list[str]] = {}
    for entry in plan.channel_slate.slate:
        wave = entry.launch_wave if isinstance(entry.launch_wave, int) else 1
        label = f"{entry.campaign_type}{f' ({entry.market})' if entry.market else ''}"
        waves.setdefault(wave, []).append(label)
    if not waves:
        return ""

    ordered = sorted(waves)
    row_height = 34
    width = 520
    height = row_height * len(ordered) + 16
    parts = [
        f'<svg class="chart-svg" viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg" role="img">'
    ]
    for index, wave in enumerate(ordered):
        y = index * row_height + 8
        parts.append(
            f'<text x="0" y="{y + 15}" font-size="10" font-weight="600" fill="{INK}">'
            f"Wave {wave}</text>"
        )
        parts.append(
            f'<line x1="58" y1="{y + 11}" x2="{width}" y2="{y + 11}" '
            f'stroke="{GRID}" stroke-width="1"/>'
        )
        offset = 62.0
        for label in waves[wave]:
            box = 8.0 + len(label) * 5.2
            if offset + box > width:
                break
            parts.append(
                f'<rect x="{_round(offset)}" y="{y + 2}" width="{_round(box)}" height="18" '
                f'rx="3" fill="{ACCENT}" fill-opacity="0.14" stroke="{ACCENT}" '
                f'stroke-width="0.6"/>'
            )
            parts.append(
                f'<text x="{_round(offset + 4)}" y="{y + 15}" font-size="9" fill="{INK}">'
                f"{_escape(label)}</text>"
            )
            offset += box + 6
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# primitives
# ---------------------------------------------------------------------------


def _paired_columns(labels: Sequence[str], bars: Sequence[float], points: Sequence[float]) -> str:
    """Columns on one scale, a point series on another, sharing an x axis."""
    width = 520
    height = 170
    left = 8
    bottom = height - 22
    plot_width = width - left - 8
    step = plot_width / max(len(labels), 1)
    bar_high = max(bars) or 1.0
    point_high = max(points) or 1.0

    parts = [
        f'<svg class="chart-svg" viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg" role="img">',
        f'<line x1="{left}" y1="{bottom}" x2="{width - 8}" y2="{bottom}" '
        f'stroke="{GRID}" stroke-width="1"/>',
    ]
    dots: list[str] = []
    for index, label in enumerate(labels):
        x = left + index * step
        bar_height = (bars[index] / bar_high) * (bottom - 26)
        parts.append(
            f'<rect x="{_round(x + step * 0.22)}" y="{_round(bottom - bar_height)}" '
            f'width="{_round(step * 0.56)}" height="{_round(bar_height)}" '
            f'fill="{ACCENT}" fill-opacity="0.35" rx="1.5"/>'
        )
        point_y = bottom - (points[index] / point_high) * (bottom - 26)
        dots.append(f"{_round(x + step / 2)},{_round(point_y)}")
        parts.append(
            f'<text x="{_round(x + step / 2)}" y="{height - 8}" font-size="8" '
            f'fill="{MUTED}" text-anchor="middle">{_escape(label)}</text>'
        )
    parts.append(
        f'<polyline points="{" ".join(dots)}" fill="none" stroke="{INK}" stroke-width="1.2"/>'
    )
    for dot in dots:
        dot_x, dot_y = dot.split(",")
        parts.append(f'<circle cx="{dot_x}" cy="{dot_y}" r="2" fill="{INK}"/>')
    parts.append("</svg>")
    return "".join(parts)


def _label(value: str) -> str:
    text = value.strip() or "—"
    return text if len(text) <= LABEL_CHARS else text[: LABEL_CHARS - 1] + "…"
