from __future__ import annotations

from collections.abc import Iterable


PALETTE = ["#cc785c", "#5db8a6", "#e8a55a", "#5db872", "#c64545", "#8e8b82", "#a9583e"]


def chart_label(value: object) -> str:
    label = str(value or "Unknown").replace("_", " ").strip().title()
    return label.replace("Qr", "QR")


def _compact_rows(rows: list[tuple[str, int]], limit: int) -> list[tuple[str, int]]:
    if len(rows) <= limit:
        return rows
    visible = rows[: limit - 1]
    other_total = sum(value for _, value in rows[limit - 1 :])
    return [*visible, ("Other", other_total)]


def donut_chart(rows: Iterable[tuple[object, int]], limit: int = 6) -> dict[str, object]:
    chart_rows = [
        (chart_label(label), int(value or 0))
        for label, value in rows
        if int(value or 0) > 0
    ]
    chart_rows.sort(key=lambda row: row[1], reverse=True)
    chart_rows = _compact_rows(chart_rows, limit)
    total = sum(value for _, value in chart_rows)

    if total <= 0:
        return {
            "total": 0,
            "gradient": "conic-gradient(var(--neutral-bg) 0 100%)",
            "slices": [],
        }

    slices = []
    gradient_parts = []
    start = 0.0
    for index, (label, value) in enumerate(chart_rows):
        percent = (value / total) * 100
        end = 100.0 if index == len(chart_rows) - 1 else start + percent
        color = PALETTE[index % len(PALETTE)]
        gradient_parts.append(f"{color} {start:.2f}% {end:.2f}%")
        slices.append(
            {
                "label": label,
                "value": value,
                "percent": round(percent),
                "color": color,
            }
        )
        start = end

    return {
        "total": total,
        "gradient": f"conic-gradient({', '.join(gradient_parts)})",
        "slices": slices,
    }


def bar_chart(rows: Iterable[tuple[object, int]], limit: int = 8, include_zero: bool = False) -> dict[str, object]:
    chart_rows = [
        (chart_label(label), int(value or 0))
        for label, value in rows
        if include_zero or int(value or 0) > 0
    ]
    if not include_zero:
        chart_rows.sort(key=lambda row: row[1], reverse=True)
        chart_rows = _compact_rows(chart_rows, limit)

    max_value = max((value for _, value in chart_rows), default=0)
    bars = []
    for index, (label, value) in enumerate(chart_rows):
        bars.append(
            {
                "label": label,
                "value": value,
                "percent": round((value / max_value) * 100) if max_value else 0,
                "color": PALETTE[index % len(PALETTE)],
            }
        )

    return {
        "total": sum(value for _, value in chart_rows),
        "bars": bars,
    }
