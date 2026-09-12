"""
Kalshi integration -- scoped to touchdown props and game-level props only
(spread/total/moneyline), per explicit instruction. Player yardage props
are intentionally left to PrizePicks (see prizepicks_client.py) so the
two sources cover different, non-overlapping prop types.

IMPORTANT: this hits Kalshi's real, documented, unauthenticated public
API (https://docs.kalshi.com/getting_started/quick_start_market_data).
No API key needed. BUT this has never been run against a live response
from the sandbox that built it -- that sandbox's network allowlist
doesn't include api.elections.kalshi.com. This has only been verified
against Kalshi's own documentation, not a live call. Treat the first
real GitHub Actions run of this as the actual test; check the workflow
log for what it found (or how it failed) and adjust from there, the
same way nflreadpy was verified live before being trusted.

Because of that, everything here fails soft: if Kalshi's response shape
doesn't match what's coded here, or the request fails outright, this
returns an empty list and the site builds anyway without Kalshi data --
never a crash, never a fabricated number.
"""

import requests

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

# Keywords used to classify discovered events client-side, since Kalshi
# doesn't publish a fixed ticker-naming lookup table -- markets are meant
# to be discovered via the /events endpoint, not guessed.
TD_KEYWORDS = ["touchdown", " td ", "score a td", "anytime td"]
GAME_KEYWORDS = ["spread", "moneyline", "total points", "over/under", "to win by", "final score"]
NFL_KEYWORDS = ["nfl", "national football league"]


def _looks_like_nfl(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in NFL_KEYWORDS) or _looks_like_team_matchup(t)


def _looks_like_team_matchup(text: str) -> bool:
    # crude fallback: "X vs Y" or "X @ Y" pattern is common in Kalshi
    # sports event titles even when "NFL" isn't spelled out.
    return " vs " in text.lower() or " vs. " in text.lower()


def fetch_nfl_touchdown_and_game_props(max_events: int = 500, timeout: int = 15) -> dict:
    """
    Returns {"touchdown_props": [...], "game_props": [...], "error": str|None}.
    Never raises -- a failure here should never break the site build.
    """
    result = {"touchdown_props": [], "game_props": [], "error": None}
    try:
        events = []
        cursor = None
        while len(events) < max_events:
            params = {"status": "open", "limit": 200, "with_nested_markets": "true"}
            if cursor:
                params["cursor"] = cursor
            resp = requests.get(f"{BASE_URL}/events", params=params, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            batch = data.get("events", [])
            events.extend(batch)
            cursor = data.get("cursor")
            if not cursor or not batch:
                break

        for event in events:
            title = event.get("title", "") or ""
            if not _looks_like_nfl(title):
                continue

            markets = event.get("markets", [])
            lower_title = title.lower()
            is_td = any(k in lower_title for k in TD_KEYWORDS)
            is_game = any(k in lower_title for k in GAME_KEYWORDS)

            for m in markets:
                m_title = m.get("title", "") or title
                m_lower = m_title.lower()
                record = {
                    "event_title": title,
                    "market_title": m_title,
                    "ticker": m.get("ticker"),
                    "yes_bid": m.get("yes_bid"),
                    "yes_ask": m.get("yes_ask"),
                    "volume": m.get("volume"),
                    "close_time": event.get("close_time") or m.get("close_time"),
                }
                if is_td or any(k in m_lower for k in TD_KEYWORDS):
                    result["touchdown_props"].append(record)
                elif is_game or any(k in m_lower for k in GAME_KEYWORDS):
                    result["game_props"].append(record)
                # anything else (e.g. yardage props) is deliberately
                # skipped -- out of scope per the Kalshi/PrizePicks split.

    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"

    return result
