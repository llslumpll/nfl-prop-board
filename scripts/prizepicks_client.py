"""
PrizePicks integration -- real player prop lines (passing/rushing/
receiving/receptions yards) to sit alongside our own projections on the
stat pages. Kalshi covers touchdowns and game-level props instead (see
kalshi_client.py); this covers everything else, per explicit instruction.

REAL, CONFIRMED ENDPOINT (not a guess): https://partner-api.prizepicks.com/projections
Confirmed via an actual scraped record published by a third-party
scraper vendor, showing this exact URL as its "sourceUrl". PrizePicks
has NO official API -- this is real, but unofficial and undocumented.

GENUINE, DOCUMENTED RISK (not speculation): the vendor selling access to
this same endpoint explicitly states they route requests through a "US
residential proxy," strongly implying PrizePicks blocks datacenter/cloud
IP ranges -- exactly what GitHub Actions runners use. This may simply
fail from Actions even though the endpoint itself is real. That's a
meaningfully different risk than Kalshi's official, documented API.

Because the exact numeric league_id for NFL isn't independently
confirmed (only guessed at in various unofficial write-ups), this does
NOT filter by league_id server-side. Instead it fetches broadly and
matches player names against our OWN known NFL roster (from
nflreadpy) -- something we're already certain about -- rather than
trusting an unverified numeric ID or a league-name string PrizePicks
might format differently than expected.

Fails soft, same as Kalshi: any failure returns an empty result with a
clear error, never breaks the build, never fabricates a line.
"""

import requests

BASE_URL = "https://partner-api.prizepicks.com/projections"

# Real browser-like headers -- reduces (does not guarantee) the odds of
# trivial bot-detection tripping on User-Agent/Accept alone. Does not
# address IP-based blocking; see module docstring.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

STAT_TYPE_MAP = {
    "pass yards": "passing_yards",
    "passing yards": "passing_yards",
    "rush yards": "rushing_yards",
    "rushing yards": "rushing_yards",
    "receiving yards": "receiving_yards",
    "receptions": "receptions",
}


def fetch_nfl_player_props(known_player_names: set[str], timeout: int = 15) -> dict:
    """
    known_player_names: the set of player_display_name values we already
    know are real 2026 NFL players (from nflreadpy) -- used to filter
    PrizePicks' full, all-sports board down to real matches, since we
    aren't confident filtering by their league_id/name field is reliable.

    Returns {"props": {player_name: {stat_col: line_value}}, "error": str|None,
    "diagnostics": {...}}.
    """
    result = {"props": {}, "error": None, "diagnostics": {}}
    try:
        resp = requests.get(
            BASE_URL,
            params={"per_page": 5000, "single_stat": "true"},
            headers=HEADERS,
            timeout=timeout,
        )
        result["diagnostics"]["status_code"] = resp.status_code
        resp.raise_for_status()
        payload = resp.json()

        data = payload.get("data", [])
        included = payload.get("included", [])
        result["diagnostics"]["total_projections_returned"] = len(data)
        result["diagnostics"]["total_included_resources"] = len(included)

        # Build id -> attributes lookups for the JSON:API "included" graph.
        players_by_id = {
            item["id"]: item.get("attributes", {})
            for item in included if item.get("type") == "new_player"
        }

        matched = 0
        sample_stat_types = set()
        for proj in data:
            attrs = proj.get("attributes", {})
            stat_type = (attrs.get("stat_type") or "").strip().lower()
            sample_stat_types.add(stat_type)
            stat_col = STAT_TYPE_MAP.get(stat_type)
            if not stat_col:
                continue

            player_rel = proj.get("relationships", {}).get("new_player", {}).get("data")
            if not player_rel:
                continue
            player_attrs = players_by_id.get(player_rel.get("id"), {})
            player_name = player_attrs.get("name")
            if not player_name or player_name not in known_player_names:
                continue

            line = attrs.get("line_score")
            if line is None:
                continue

            result["props"].setdefault(player_name, {})[stat_col] = line
            matched += 1

        result["diagnostics"]["matched_props"] = matched
        result["diagnostics"]["sample_stat_types_seen"] = sorted(sample_stat_types)[:20]

    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"

    return result
