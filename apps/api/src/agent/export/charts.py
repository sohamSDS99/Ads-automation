"""Inline SVG charts for the printed report (PRD §12).

§12 asks for charts "pre-rendered as inline SVG". It names matplotlib's svg
backend; this builds the SVG directly instead, and the reason is the one
property this whole phase is organised around. matplotlib's SVG output embeds
font metrics and glyph paths, so the same report rendered on two machines — or
on two matplotlib releases — produces different bytes. That makes the PDF
non-reproducible and the golden tests unwritable, to buy plotting power that two
bar charts and a twelve-point sparkline do not need. It also keeps numpy,
pillow, fonttools and kiwisolver out of both images.

**This is a deviation from §12's named library and wants a ruling.** Swapping
matplotlib back in is this one module; nothing else imports it.

Every chart is drawn from numbers already in the report. Nothing here computes a
finding — an axis is scaled, and that is the extent of the judgement.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from agent.export.contract import ResearchReport
from agent.export.templating import MONTH_NAMES

#: Print-safe ink. The report is read on paper as often as on screen, so the
#: charts have to survive being photocopied: shape carries the meaning, colour
#: only reinforces it.
INK = "#1f2933"
MUTED = "#8c9196"
ACCENT = "#0b6b5e"
GRID = "#d8dcdf"


def _round(value: float) -> str:
    """Two decimal places, no trailing zeroes, no locale, no `-0`.

    Coordinate formatting is where a "deterministic" renderer usually stops
    being one: `repr(float)` differs across platforms at the last digit.
    """
    rendered = f"{value:.2f}".rstrip("0").rstrip(".")
    return "0" if rendered in {"", "-0"} else rendered


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


@dataclass(frozen=True, slots=True)
class Chart:
    """One rendered figure, ready to drop into the print HTML."""

    title: str
    svg: str
    caption: str = ""


def sparkline(
    values: Sequence[float],
    *,
    labels: Sequence[str] | None = None,
    width: int = 520,
    height: int = 120,
    baseline: float | None = 1.0,
) -> str:
    """A twelve-point seasonality curve.

    `baseline` draws the "average month" line, which is the only reference that
    makes a seasonality index readable: without it a curve between 0.57 and 1.27
    looks like noise rather than a 2.2× swing.
    """
    if not values:
        return ""

    pad_left, pad_right, pad_top, pad_bottom = 4, 4, 10, 18
    plot_width = width - pad_left - pad_right
    plot_height = height - pad_top - pad_bottom

    low = min(list(values) + ([baseline] if baseline is not None else []))
    high = max(list(values) + ([baseline] if baseline is not None else []))
    span = high - low or 1.0

    def x_at(index: int) -> float:
        if len(values) == 1:
            return pad_left + plot_width / 2
        return pad_left + plot_width * index / (len(values) - 1)

    def y_at(value: float) -> float:
        return pad_top + plot_height * (1 - (value - low) / span)

    points = " ".join(f"{_round(x_at(i))},{_round(y_at(v))}" for i, v in enumerate(values))

    parts = [
        f'<svg class="chart-svg" viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg" role="img">'
    ]
    if baseline is not None and low <= baseline <= high:
        y = _round(y_at(baseline))
        parts.append(
            f'<line x1="{pad_left}" y1="{y}" x2="{_round(pad_left + plot_width)}" y2="{y}" '
            f'stroke="{GRID}" stroke-width="1" stroke-dasharray="3 3"/>'
        )
    parts.append(
        f'<polyline fill="none" stroke="{ACCENT}" stroke-width="2" '
        f'stroke-linejoin="round" points="{points}"/>'
    )
    for index, value in enumerate(values):
        parts.append(
            f'<circle cx="{_round(x_at(index))}" cy="{_round(y_at(value))}" r="2" fill="{ACCENT}"/>'
        )
    tick_labels = labels if labels is not None else MONTH_NAMES[: len(values)]
    for index, label in enumerate(tick_labels):
        parts.append(
            f'<text x="{_round(x_at(index))}" y="{height - 4}" font-size="9" fill="{MUTED}" '
            f'text-anchor="middle">{_escape(label)}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def bar_chart(
    labels: Sequence[str],
    values: Sequence[float],
    *,
    value_labels: Sequence[str] | None = None,
    width: int = 520,
    row_height: int = 26,
    label_width: int = 150,
) -> str:
    """Horizontal bars — the only orientation where a long label stays readable."""
    if not labels or not values:
        return ""

    height = row_height * len(labels) + 8
    plot_width = width - label_width - 70
    high = max(values) or 1.0

    parts = [
        f'<svg class="chart-svg" viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg" role="img">'
    ]
    for index, (label, value) in enumerate(zip(labels, values, strict=False)):
        y = index * row_height + 4
        bar = plot_width * (value / high) if high else 0.0
        display = value_labels[index] if value_labels else _round(value)
        parts.append(
            f'<text x="0" y="{y + row_height // 2 + 3}" font-size="10" fill="{INK}">'
            f"{_escape(label)}</text>"
        )
        parts.append(
            f'<rect x="{label_width}" y="{y + 4}" width="{_round(bar)}" '
            f'height="{row_height - 12}" fill="{ACCENT}" rx="2"/>'
        )
        parts.append(
            f'<text x="{_round(label_width + bar + 6)}" y="{y + row_height // 2 + 3}" '
            f'font-size="10" fill="{MUTED}">{_escape(str(display))}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _weighted_seasonality(report: ResearchReport) -> list[float]:
    """One demand curve for the account, weighted by search volume.

    Weighting matters: an unweighted mean lets 200 low-volume long-tail terms
    outvote the handful of terms the budget will actually go to.
    """
    totals = [0.0] * 12
    weight_total = 0.0
    for keyword in report.priced_keyword_list:
        curve = keyword.seasonality_index
        if len(curve) != 12:
            continue
        weight = float(keyword.volume or 0) or 1.0
        for month in range(12):
            totals[month] += curve[month] * weight
        weight_total += weight
    if not weight_total:
        return []
    return [round(total / weight_total, 4) for total in totals]


def build_charts(report: ResearchReport) -> list[Chart]:
    """Every figure the printed report carries, in the order it carries them.

    Returns an empty list rather than placeholders when there is nothing to
    plot. An axis with no data on it is worse than a missing figure: it looks
    like a measurement of zero.
    """
    charts: list[Chart] = []

    curve = _weighted_seasonality(report)
    if curve:
        peak = MONTH_NAMES[curve.index(max(curve))]
        trough = MONTH_NAMES[curve.index(min(curve))]
        charts.append(
            Chart(
                title="Demand through the year",
                svg=sparkline(curve),
                caption=(
                    f"Volume-weighted across {len(report.priced_keyword_list)} priced keywords. "
                    f"Peaks in {peak}, bottoms out in {trough}. 1.0 is an average month."
                ),
            )
        )

    scenarios = report.readiness.scenarios
    if scenarios:
        charts.append(
            Chart(
                title="Conversions by monthly budget",
                svg=bar_chart(
                    [f"{scenario.budget_usd_month:,.0f} USD/mo" for scenario in scenarios],
                    [scenario.est_conv or 0.0 for scenario in scenarios],
                    value_labels=[
                        f"{scenario.est_conv or 0:,.1f} conv"
                        + (f" @ {scenario.est_cpa:,.0f} CPA" if scenario.est_cpa else "")
                        for scenario in scenarios
                    ],
                ),
                caption="Modelled, not measured. Each scenario states its own assumptions above.",
            )
        )

    clusters = report.competitive_landscape.message_clusters
    if clusters:
        ranked = sorted(clusters, key=lambda c: (-(c.frequency or 0), c.theme))[:8]
        charts.append(
            Chart(
                title="What competitors are saying",
                svg=bar_chart(
                    [cluster.theme for cluster in ranked],
                    [float(cluster.frequency or 0) for cluster in ranked],
                    value_labels=[f"{cluster.frequency or 0} ads" for cluster in ranked],
                ),
                caption="Message themes by ad count across the captured creative corpus.",
            )
        )

    return charts
