"""The inline SVG charts (PRD §12).

These exist because the charts are built by hand rather than by matplotlib, and
the reason given for that was determinism. A claim like that needs a test, or it
is just a preference with a paragraph attached.
"""

from __future__ import annotations

from agent.export.charts import _weighted_seasonality, bar_chart, build_charts, sparkline
from tests.report_support import golden_report, minimal_report


def test_charts_are_byte_identical_across_renders() -> None:
    """The property matplotlib could not give us."""
    first = [(chart.title, chart.svg) for chart in build_charts(golden_report())]
    second = [(chart.title, chart.svg) for chart in build_charts(golden_report())]
    assert first == second


def test_the_golden_report_produces_all_three_figures() -> None:
    titles = [chart.title for chart in build_charts(golden_report())]
    assert titles == [
        "Demand through the year",
        "Conversions by monthly budget",
        "What competitors are saying",
    ]


def test_an_empty_report_produces_no_figures() -> None:
    """An axis with no data looks like a measurement of zero."""
    assert build_charts(minimal_report()) == []


def test_seasonality_is_weighted_by_volume() -> None:
    """An unweighted mean lets the long tail outvote the terms the budget goes to."""
    report = golden_report()
    curve = _weighted_seasonality(report)

    assert len(curve) == 12
    # The 880-volume term peaks in October; the curve should follow it, not the
    # 260-volume term that has no curve at all.
    assert curve.index(max(curve)) == 9
    assert all(0.4 < value < 1.6 for value in curve)


def test_keywords_without_a_full_curve_are_excluded_not_zero_filled() -> None:
    report = golden_report()
    with_gap = _weighted_seasonality(report)

    report.priced_keyword_list = [
        keyword for keyword in report.priced_keyword_list if keyword.seasonality_index
    ]
    without_gap = _weighted_seasonality(report)
    assert with_gap == without_gap


def test_svg_is_self_contained_and_escaped() -> None:
    svg = bar_chart(['Audit & "readiness" <b>', "Time saved"], [7.0, 3.0])
    assert svg.startswith("<svg")
    assert "&amp;" in svg and "&quot;" in svg and "&lt;b&gt;" in svg
    assert "<b>" not in svg

    # Not "no http": the SVG namespace is a http:// URI and is required. What
    # must be absent is anything that *fetches* — WeasyPrint would try, and a
    # chart that needs the network is not an inline chart.
    for reference in ("<image", "xlink:href", "href=", "url(", "<script"):
        assert reference not in svg, f"a chart reached outside itself: {reference}"


def test_a_sparkline_with_no_data_renders_nothing() -> None:
    assert sparkline([]) == ""
    assert bar_chart([], []) == ""


def test_a_single_point_sparkline_does_not_divide_by_zero() -> None:
    assert sparkline([1.0]).startswith("<svg")


def test_a_flat_curve_does_not_divide_by_zero() -> None:
    """Every month identical is a real input, and `high - low` is then zero."""
    assert sparkline([1.0] * 12, baseline=1.0).startswith("<svg")


def test_coordinates_carry_no_platform_specific_float_repr() -> None:
    """Full float repr and scientific notation are both platform-shaped."""
    import re

    svg = sparkline([1.0, 1.0 / 3.0, 2.0 / 3.0])
    assert "0.3333333" not in svg

    # Check the NUMBERS, not the whole string: "e-" also matches "stroke-".
    numbers = re.findall(r'(?:x|y|cx|cy|x1|y1|x2|y2|width|height)="([^"]+)"', svg)
    assert numbers
    for number in numbers:
        assert "e" not in number.lower(), f"coordinate in scientific notation: {number}"
        assert len(number.split(".")[-1]) <= 2 or "." not in number
