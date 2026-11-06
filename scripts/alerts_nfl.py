"""
Discord alerts (ported from the NBA site, Nov 2026).

Setup: Discord channel -> Edit Channel -> Integrations -> Webhooks -> New Webhook -> Copy Webhook URL, then this
repo -> Settings -> Secrets and variables -> Actions -> New repository secret DISCORD_WEBHOOK. No secret = no
alerts (nothing breaks). The URL lives only in that secret.

What gets sent (each once; data/alerts_state.json remembers what went out):
  * injury news: a starter newly ruled OUT, with the teammate OVER plays (news_nfl.py)
  * Sunday card (first build after 9 AM CT Sunday): this week's Best 5 still open, the PROVEN picks whose line
    hasn't moved yet (bet early), and the site's parlays with units
  * week recap, once every pick of the week is graded: props, Best 5, game picks, parlays (units and dollars,
    1U = $100), news plays
  * bet early: when a PROVEN kind of pick gets its first line and it hasn't moved yet (each pick once)
  * a one-time "connected" message the first time the secret works
"""

from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent.parent / "data"
STATE = ROOT / "alerts_state.json"
SITE = "https://llslumpll.github.io/nfl-prop-board/"
CT = ZoneInfo("America/Chicago")
UNIT_USD = 100
MAX_LEN = 1900
SHORT = {"passing_yards": "PASS YDS", "rushing_yards": "RUSH YDS", "receiving_yards": "REC YDS", "receptions": "REC"}


def _url() -> str | None:
    u = (os.environ.get("DISCORD_WEBHOOK") or "").strip()
    return u if u.startswith("https://") else None


def _post(url: str, text: str) -> bool:
    body = json.dumps({"content": text[:MAX_LEN], "username": "Prop Edge NFL", "allowed_mentions": {"parse": []}}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "User-Agent": "prop-edge-alerts"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return 200 <= r.status < 300
    except Exception as e:  # noqa: BLE001
        print(f"Alert not sent: {str(e)[:120]}")
        return False


def _money(u: float) -> str:
    return f"{u:+.2f}U ({'+' if u >= 0 else '-'}${abs(u) * UNIT_USD:,.0f})"


def _rec(rows, key="hit") -> str:
    h = sum(1 for r in rows if r.get(key))
    return f"{h}/{len(rows)} ({100 * h / len(rows):.0f}%)" if rows else "none"


def news_messages(week: int | None) -> list[tuple[str, str]]:
    import news_nfl
    out = []
    for ev in news_nfl.load().get(str(week), {}).values():
        plays = [b for b in ev["plays"] if b.get("result") is None and not b.get("moved")]
        if not plays:
            continue
        lines = [f"🚨 **{ev['out']} ({ev['team']}) ruled OUT** vs {ev['opp']}",
                 "Teammates who did better without him (lines as of the news):"]
        for b in plays[:6]:
            lines.append(f"• **{b['player']} OVER {b['line']:g} {b['short']}**: {b['without']} without him vs {b['with']} with "
                         f"({b['n_wo']} game{'s' if b['n_wo'] != 1 else ''})")
        s = news_nfl.summary()
        lines.append(f"News plays so far: {s['hits']}/{s['n']} · tracking only, no stakes")
        lines.append(SITE + "games.html")
        out.append((f"news|{week}|{ev['out']}|{ev['team']}", "\n".join(lines)))
    return out


def sunday_card(week: int | None, now: datetime) -> list[tuple[str, str]]:
    local = now.astimezone(CT)
    if week is None or local.weekday() != 6 or local.hour < 9:
        return []
    import grade
    import line_movement_log
    import parlays_nfl
    import predictions
    preds = [p for p in predictions.load_predictions().values() if p["week"] == week and not p.get("graded")
             and p["stat"] in SHORT and p.get("market_line") is not None and p.get("call") in ("OVER", "UNDER")]
    rec = grade.pick_records()
    log = line_movement_log._load()
    b5 = [p for p in preds if "highest_confidence" in (p.get("best5_tags") or [])]
    proven = []
    for p in preds:
        r = rec.get(f"{p['stat']}|{p['call']}")
        e = log.get(f"{p['player']}|{p['stat']}|{week}")
        if r and r["n"] >= 30 and r["rate"] >= 54.2 and (not e or e["current_line"] == p["market_line"]):
            proven.append((r["rate"], p))
    lines = [f"🏈 **Week {week} card**"]
    if b5:
        lines.append("**Best 5:**")
        lines += [f"• {p['player']} {p['call']} {p['market_line']:g} {SHORT[p['stat']]}" for p in b5[:10]]
    if proven:
        lines.append("**★ Proven, line hasn't moved (bet early):**")
        lines += [f"• {p['player']} {p['call']} {p['market_line']:g} {SHORT[p['stat']]} ({r:.0f}% for picks like this)"
                  for r, p in sorted(proven, key=lambda x: -x[0])[:8]]
    out = []
    if len(lines) > 1:
        out.append((f"card|{week}", "\n".join(lines + [SITE])))
    lines = [f"🎟️ **Week {week} parlays** (locked at first kickoff)"]
    pz = parlays_nfl.load().get(str(week), {})
    names = {"props": "safest props", "value": "good-price props", "ml": "moneyline", "td": "touchdown"}
    for pid in ("props-2", "value-2", "ml-2", "ml-3", "td-1", "td-2", "td-3", "td-4"):
        par = pz.get(pid)
        if par and par["status"] == "pending" and par.get("decimal"):
            kind = "single" if par["size"] == 1 else f"{par['size']}-leg"
            legs = " + ".join(l["label"] for l in par["legs"])
            gp = " · GOOD PRICE" if par.get("good") else ""
            lines.append(f"• {kind} {names.get(par['family'], par['family'])}: {legs} · {par['stake']:g}U at "
                         f"{par['decimal']:.2f}x · {100 * par['chance']:.0f}% to cash{gp}")
    if len(lines) > 1:
        out.append((f"parlays|{week}", "\n".join(lines + [SITE + "parlays.html"])))
    return out


def early_lines(week: int | None, now: datetime, sent: dict) -> tuple[list[str], str] | None:
    """Bet-early alert: a PROVEN kind of pick whose line just posted and hasn't moved yet, before kickoff. Picks the
    market later moved toward hit 61.8% at the line we picked, so the value is in getting the first line. One
    message per build with every new one (each pick alerted once)."""
    if week is None:
        return None
    import dataio
    import grade
    import line_attach
    import line_movement_log
    import predictions
    rec = grade.pick_records()
    log = line_movement_log._load()
    sched = dataio.load_schedule()
    new = []
    for p in predictions.load_predictions().values():
        if p["week"] != week or p.get("graded") or p["stat"] not in SHORT or p.get("market_line") is None \
                or p.get("call") not in ("OVER", "UNDER"):
            continue
        key = f"early|{week}|{p['player']}|{p['stat']}"
        if key in sent:
            continue
        r = rec.get(f"{p['stat']}|{p['call']}")
        if not r or r["n"] < 30 or r["rate"] < 54.2:
            continue
        e = log.get(f"{p['player']}|{p['stat']}|{week}")
        if e and e["current_line"] != p["market_line"]:
            continue
        k = line_attach._kickoff_utc(p["team"], week, sched)
        if k is None or now >= k:
            continue
        new.append((key, r, p, k))
    if not new:
        return None
    new.sort(key=lambda x: (-x[1]["rate"], x[3], x[2]["player"]))
    lines = [f"⏰ **Bet early: {len(new)} new line{'s' if len(new) != 1 else ''} on proven picks** (Week {week})",
             "These kinds of picks have beaten break-even all season, and the line hasn't moved yet:"]
    shown = 0
    for key, r, p, k in new:
        row = (f"• **{p['player']} {p['call']} {p['market_line']:g} {SHORT[p['stat']]}** · {p['team']} "
               f"{k.astimezone(CT):%a %-I:%M %p} · {p['call'].lower()}s like this {r['rate']:.0f}% ({r['n']})")
        if sum(len(x) + 1 for x in lines) + len(row) > MAX_LEN - 120:
            break
        lines.append(row)
        shown += 1
    if shown < len(new):
        lines.append(f"+{len(new) - shown} more on the Picks page")
    lines.append(SITE + "picks.html")
    return [x[0] for x in new], "\n".join(lines)


def recap_message(now: datetime) -> tuple[str, str] | None:
    """The most recent week whose picks with lines are all graded, once."""
    import news_nfl
    import parlays_nfl
    import predictions
    import team_predictions
    allp = [p for p in predictions.load_predictions().values() if p["stat"] in SHORT and p.get("market_line") is not None
            and p.get("call") in ("OVER", "UNDER")]
    weeks = sorted({p["week"] for p in allp})
    done = [w for w in weeks if all(p.get("graded") for p in allp if p["week"] == w)]
    if not done:
        return None
    w = done[-1]
    g = [p for p in allp if p["week"] == w and p.get("hit") is not None and not p.get("void")]
    b5 = [p for p in g if "highest_confidence" in (p.get("best5_tags") or [])]
    lines = [f"🌅 **Week {w} recap**", f"Player picks: {_rec(g)} · Best 5: {_rec(b5)}"]
    gp = [t for t in team_predictions.load_team_predictions().values() if t["week"] == w and t.get("graded")
          and t.get("our_correct") is not None]
    if gp:
        lines.append(f"Game picks: {_rec(gp, 'our_correct')}")
    pz = [p for p in parlays_nfl.load().get(str(w), {}).values() if p["status"] in ("hit", "miss")]
    if pz:
        u = sum(p.get("units") or 0 for p in pz if p.get("units") is not None)
        priced = any(p.get("units") is not None for p in pz)
        lines.append(f"Parlays: {sum(1 for p in pz if p['status'] == 'hit')}/{len(pz)} cashed"
                     + (f" · {_money(u)}" if priced else ""))
    nd = [b for ev in news_nfl.load().get(str(w), {}).values() for b in ev["plays"] if b.get("result") in ("hit", "miss")]
    if nd:
        lines.append(f"News plays: {sum(1 for b in nd if b['result'] == 'hit')}/{len(nd)}")
    lines.append(SITE)
    return (f"recap|{w}", "\n".join(lines))


def run(week: int | None, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    url = _url()
    if not url:
        return {"enabled": False}
    try:
        st = json.loads(STATE.read_text())
    except Exception:  # noqa: BLE001
        st = {"sent": {}}
    sent = st.setdefault("sent", {})
    msgs = []
    if not st.get("connected"):
        msgs.append(("connected", "✅ Prop Edge NFL alerts are connected. You'll get injury-news plays, the Sunday card "
                                  f"and a recap when each week is graded.\n{SITE}"))
    for fn in (lambda: news_messages(week), lambda: sunday_card(week, now), lambda: [recap_message(now)]):
        try:
            msgs += [m for m in fn() if m]
        except Exception as e:  # noqa: BLE001
            print(f"Alert check failed: {e}")
    n = 0
    stamp = now.strftime("%Y-%m-%d %H:%M UTC")
    try:
        el = early_lines(week, now, sent)
    except Exception as e:  # noqa: BLE001
        print(f"Alert check failed: {e}")
        el = None
    if el and st.get("connected") and _post(url, el[1]):
        for k in el[0]:
            sent[k] = stamp
        n += 1
    for key, text in msgs:
        if key in sent:
            continue
        if _post(url, text):
            sent[key] = stamp
            n += 1
            if key == "connected":
                st["connected"] = stamp
    cut = (now - timedelta(days=60)).strftime("%Y-%m-%d")
    st["sent"] = {k: v for k, v in sent.items() if v[:10] >= cut}
    STATE.write_text(json.dumps(st, indent=1, sort_keys=True))
    return {"enabled": True, "sent": n}
