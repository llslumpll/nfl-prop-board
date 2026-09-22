"""
Minimal SVG line chart generator, server-side, no JS charting library
needed -- matches this site's fully-static approach. Renders a single
or multi-series line chart with an optional dashed reference line
(e.g. 50% = coin flip), styled to match the cyber grid theme via CSS
custom properties already defined in style.css.
"""


def line_chart(
    series: list[dict],
    width: int = 640,
    height: int = 220,
    y_min: float = 0,
    y_max: float = 100,
    reference_line: float | None = None,
    x_labels: tuple[str, str] | None = None,
) -> str:
    """
    series: [{"points": [y0, y1, ...], "color": "#00f0ff"}, ...]
    All series share the same x-axis (evenly spaced by index).
    Returns a raw <svg> string.
    """
    pad_l, pad_r, pad_t, pad_b = 42, 16, 16, 28
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    def x_at(i, n):
        return pad_l + (plot_w * i / max(1, n - 1)) if n > 1 else pad_l

    def y_at(v):
        v = max(y_min, min(y_max, v))
        return pad_t + plot_h - (plot_h * (v - y_min) / (y_max - y_min) if y_max > y_min else 0)

    parts = [f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto;">']

    # gridlines + y labels (0/50/100 style, 4 ticks)
    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        val = y_min + (y_max - y_min) * frac
        y = y_at(val)
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" stroke="rgba(255,255,255,0.06)" stroke-width="1"/>')
        parts.append(f'<text x="{pad_l - 8}" y="{y + 4:.1f}" text-anchor="end" font-size="10" fill="#767676" font-family="monospace">{val:.0f}</text>')

    if reference_line is not None:
        ry = y_at(reference_line)
        parts.append(f'<line x1="{pad_l}" y1="{ry:.1f}" x2="{width - pad_r}" y2="{ry:.1f}" stroke="#f0f0ee" stroke-width="1.5" stroke-dasharray="5,4"/>')

    for s in series:
        pts = s.get("points", [])
        color = s.get("color", "#00f0ff")
        n = len(pts)
        if n == 0:
            continue
        path = " ".join(
            f'{"M" if i == 0 else "L"}{x_at(i, n):.1f},{y_at(v):.1f}'
            for i, v in enumerate(pts) if v is not None
        )
        parts.append(f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2"/>')
        for i, v in enumerate(pts):
            if v is None:
                continue
            parts.append(f'<circle cx="{x_at(i, n):.1f}" cy="{y_at(v):.1f}" r="3" fill="{color}"/>')

    if x_labels:
        parts.append(f'<text x="{pad_l}" y="{height - 6}" font-size="10" fill="#767676" font-family="monospace">{x_labels[0]}</text>')
        parts.append(f'<text x="{width - pad_r}" y="{height - 6}" text-anchor="end" font-size="10" fill="#767676" font-family="monospace">{x_labels[1]}</text>')

    parts.append("</svg>")
    return "".join(parts)


def bar_chart_with_threshold(
    bars: list[dict],
    width: int = 640,
    height: int = 200,
    y_min: float = 0,
    y_max: float | None = None,
) -> str:
    """
    Real per-game performance-vs-line chart, PrizePicks-style but grown
    to the whole season instead of a fixed last-5: one bar per real game
    played, bar height = the real actual result that game, with a short
    horizontal tick showing exactly where the real market line was set
    that day -- so it's immediately visible whether a line was set high
    or low relative to recent form, not just whether it hit.

    bars: [{"label": "W1", "value": 205, "threshold": 220.5 | None,
            "status": "hit" | "miss" | "pending" | "no_line"}, ...]
    """
    pad_l, pad_r, pad_t, pad_b = 34, 12, 14, 24
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    n = len(bars)

    COLORS = {
        "hit": "#3ddc97", "miss": "#ff3b5c",
        "pending": "#ff6a3d", "no_line": "#3a3a3a",
    }

    values = [b["value"] for b in bars if b.get("value") is not None]
    thresholds = [b["threshold"] for b in bars if b.get("threshold") is not None]
    all_vals = values + thresholds
    if y_max is None:
        y_max = max(all_vals) * 1.15 if all_vals else 100

    def y_at(v):
        v = max(y_min, min(y_max, v))
        return pad_t + plot_h - (plot_h * (v - y_min) / (y_max - y_min) if y_max > y_min else 0)

    bar_w = plot_w / max(1, n) * 0.62
    gap = plot_w / max(1, n)

    parts = [f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto;">']

    for frac in (0, 0.5, 1.0):
        val = y_min + (y_max - y_min) * frac
        y = y_at(val)
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" stroke="rgba(255,255,255,0.06)" stroke-width="1"/>')
        parts.append(f'<text x="{pad_l - 6}" y="{y + 3:.1f}" text-anchor="end" font-size="9" fill="#767676" font-family="monospace">{val:.0f}</text>')

    for i, b in enumerate(bars):
        cx = pad_l + gap * i + gap / 2
        color = COLORS.get(b.get("status"), "#3a3a3a")
        val = b.get("value")
        if val is not None:
            by = y_at(val)
            parts.append(f'<rect x="{cx - bar_w/2:.1f}" y="{by:.1f}" width="{bar_w:.1f}" height="{(pad_t + plot_h - by):.1f}" fill="{color}" opacity="0.85" rx="2"/>')
        thr = b.get("threshold")
        if thr is not None:
            ty = y_at(thr)
            parts.append(f'<line x1="{cx - bar_w/2 - 3:.1f}" y1="{ty:.1f}" x2="{cx + bar_w/2 + 3:.1f}" y2="{ty:.1f}" stroke="#f0f0ee" stroke-width="2"/>')
        parts.append(f'<text x="{cx:.1f}" y="{height - 6}" text-anchor="middle" font-size="9" fill="#767676" font-family="monospace">{b.get("label","")}</text>')

    parts.append("</svg>")
    return "".join(parts)
