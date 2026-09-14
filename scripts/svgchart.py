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
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" stroke="rgba(0,240,255,0.08)" stroke-width="1"/>')
        parts.append(f'<text x="{pad_l - 8}" y="{y + 4:.1f}" text-anchor="end" font-size="10" fill="#6c8a94" font-family="monospace">{val:.0f}</text>')

    if reference_line is not None:
        ry = y_at(reference_line)
        parts.append(f'<line x1="{pad_l}" y1="{ry:.1f}" x2="{width - pad_r}" y2="{ry:.1f}" stroke="#ffb020" stroke-width="1" stroke-dasharray="4,3"/>')

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
        parts.append(f'<text x="{pad_l}" y="{height - 6}" font-size="10" fill="#6c8a94" font-family="monospace">{x_labels[0]}</text>')
        parts.append(f'<text x="{width - pad_r}" y="{height - 6}" text-anchor="end" font-size="10" fill="#6c8a94" font-family="monospace">{x_labels[1]}</text>')

    parts.append("</svg>")
    return "".join(parts)
