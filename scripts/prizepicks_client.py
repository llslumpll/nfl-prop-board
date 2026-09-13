"""
PrizePicks integration -- real player prop lines (passing/rushing/
receiving/receptions yards) to sit alongside our own projections on the
stat pages. Kalshi covers touchdowns and game-level props instead (see
kalshi_client.py); this covers everything else, per explicit instruction.

REWRITTEN to match the MLB site's proven-working pattern, after the
first version (broad-scan-and-match-by-roster-name across all 47,000+
projections on every sport PrizePicks covers) turned out to have two
real problems the MLB reference code already solved:
  1. No league scoping at all -- matching purely by name against every
     sport's projections is fragile and unnecessarily slow. The MLB
     script fetches /leagues first and finds the right league_id by
     name (not a hardcoded guess), then only pulls that league's
     projections. Doing the same here.
  2. Never excluded PrizePicks' "Goblin"/"Demon" alternate lines
     (easier/harder versions of the same prop) -- meaning some of the
     "matched" lines in the first version could have silently been an
     alternate line instead of the real standard one. Now filtered to
     odds_type == "standard" only, same as the MLB script.

Real, confirmed endpoint: https://partner-api.prizepicks.com -- no
official public API, unofficial and undocumented, but real (confirmed
working live from GitHub Actions, no IP blocking observed in practice).
Fails soft: any failure returns an empty result with a clear error,
never breaks the build, never fabricates a line.
"""

import requests

BASE_URL = "https://partner-api.prizepicks.com"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

# Substrings/exact-matches for PrizePicks' stat_type field. Exact match
# used where a substring would collide with an unrelated real stat type
# (mirrors the MLB script's documented "Hits" vs "Hits Allowed" and
# "Pitcher Strikeouts" vs "Hitter Strikeouts" collision fixes) --
# "Receiving Yards" is a real risk here too: PrizePicks may also offer
# combo stats like "Rush+Rec Yards" or "Pass+Rush Yards" that would
# wrongly match a naive substring check against "yards" alone.
STAT_TYPE_EXACT_MAP = {
    "pass yards": "passing_yards",
    "passing yards": "passing_yards",
    "rush yards": "rushing_yards",
    "rushing yards": "rushing_yards",
    "receiving yards": "receiving_yards",
    "receptions": "receptions",
}


def _get(url, params=None, timeout=15):
    resp = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _fetch_leagues():
    try:
        data = _get(f"{BASE_URL}/leagues")
        return data.get("data") or []
    except Exception:
        return []


def _find_nfl_league_id(leagues):
    for league in leagues:
        name = (league.get("attributes") or {}).get("name", "")
        if name.strip().upper() == "NFL":
            return league["id"]
    for league in leagues:
        name = (league.get("attributes") or {}).get("name", "")
        if "nfl" in name.lower():
            return league["id"]
    return None


def fetch_nfl_player_props(timeout: int = 15) -> dict:
    """
    Returns {"props": {player_name: {stat_col: line_value}}, "error": str|None,
    "diagnostics": {...}}.

    No longer takes a known-player-names set -- once scoped to the real
    NFL league_id, every player returned genuinely is an NFL player, so
    cross-checking against our own roster is redundant and would only
    risk dropping real rookies our historical data doesn't have yet.
    """
    result = {"props": {}, "error": None, "diagnostics": {}}
    try:
        leagues = _fetch_leagues()
        result["diagnostics"]["total_leagues"] = len(leagues)
        if not leagues:
            result["error"] = "no leagues returned from /leagues"
            return result

        league_id = _find_nfl_league_id(leagues)
        result["diagnostics"]["nfl_league_id"] = league_id
        if league_id is None:
            result["diagnostics"]["sample_league_names"] = [
                (lg.get("attributes") or {}).get("name") for lg in leagues[:20]
            ]
            result["error"] = "could not find an NFL league in /leagues response"
            return result

        payload = _get(f"{BASE_URL}/projections", params={"league_id": league_id, "per_page": 1000})
        projections = payload.get("data") or []
        included = payload.get("included") or []
        result["diagnostics"]["total_projections_returned"] = len(projections)

        players_by_id = {
            item["id"]: (item.get("attributes") or {}).get("name")
            for item in included if item.get("type") == "new_player"
        }

        stat_types_seen = set()
        odds_types_seen = set()
        matched = 0
        excluded_alt_lines = 0

        for proj in projections:
            attrs = proj.get("attributes", {})
            stat_type = (attrs.get("stat_type") or "").strip().lower()
            stat_types_seen.add(stat_type)

            odds_type = str(attrs.get("odds_type") or attrs.get("type") or "standard").strip().lower()
            odds_types_seen.add(odds_type)
            if odds_type not in ("standard", ""):
                excluded_alt_lines += 1
                continue

            stat_col = STAT_TYPE_EXACT_MAP.get(stat_type)
            if not stat_col:
                continue

            player_rel = proj.get("relationships", {}).get("new_player", {}).get("data")
            if not player_rel:
                continue
            player_name = players_by_id.get(player_rel.get("id"))
            if not player_name:
                continue

            line = attrs.get("line_score")
            if line is None:
                continue

            result["props"].setdefault(player_name, {})[stat_col] = line
            matched += 1

        result["diagnostics"]["matched_props"] = matched
        result["diagnostics"]["excluded_goblin_demon_lines"] = excluded_alt_lines
        result["diagnostics"]["sample_stat_types_seen"] = sorted(stat_types_seen)[:30]
        result["diagnostics"]["sample_odds_types_seen"] = sorted(odds_types_seen)

    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"

    return result
