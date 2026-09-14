"""
Best 5 rankings for the Home page -- ports the MLB site's edge concept
(model probability/projection vs. real market price) to NFL, using
whatever real market data (PrizePicks lines, Kalshi TD prices) is
available for a given build. No edge is ever shown without a real market
number behind it -- same "edgeEligible" honesty rule as the MLB site's
fetch_kalshi.py: a missing/thin market means no edge shown, not a
guessed one.
"""

import math


def poisson_prob_at_least(threshold: int, lam: float) -> float:
    """Same formula as the MLB site's fetch_kalshi.py -- probability a
    Poisson-distributed count is >= threshold, given mean lam. Used here
    to convert our point projection (e.g. "1.4 projected TDs") into a
    probability of clearing a specific TD threshold, comparable to
    Kalshi's real "1+ touchdowns" market price."""
    if lam <= 0:
        return 0.0 if threshold > 0 else 1.0
    cum = 0.0
    for k in range(threshold):
        cum += math.exp(-lam + k * math.log(lam) - sum(math.log(i) for i in range(2, k + 1)))
    return max(0.001, min(0.999, 1 - cum))


def best5_yardage(rows: list[dict], pp_props: dict, stat_col: str, unit: str) -> list[dict]:
    """
    rows: output of a leaders function (passing_leaders, etc.) -- each
    row must have 'player', 'team_badge', 'next_game' (with a
    .projection.projected value).
    pp_props: {player_name: {stat_col: line}} from prizepicks_client.

    Edge = our projection - PrizePicks' real line, in the stat's own
    units (yards, receptions). Ranked by |edge|, largest first. Only
    players with BOTH a real next-game projection AND a real PrizePicks
    line are eligible -- no edge is invented for a player missing either.
    """
    candidates = []
    for r in rows:
        next_game = r.get("next_game")
        if not next_game:
            continue
        pp_line = pp_props.get(r["player"], {}).get(stat_col)
        if pp_line is None:
            continue
        projected = next_game["projection"]["projected"]
        edge = round(projected - pp_line, 1)
        candidates.append({
            "player": r["player"],
            "team_badge": r.get("team_badge"),
            "opponent": next_game["opponent"],
            "week": next_game["week"],
            "projected": projected,
            "market_line": pp_line,
            "edge": edge,
            "call": "OVER" if edge > 0 else "UNDER",
            "unit": unit,
        })
    candidates.sort(key=lambda c: abs(c["edge"]), reverse=True)
    return candidates[:5]


def find_kalshi_1plus_td_price(kalshi_td_props: list[dict], player_name: str) -> float | None:
    """
    Shared lookup: finds this player's real Kalshi "1+ touchdowns"
    market price, matched by name (Kalshi's title format is literally
    "{Name}: 1+ touchdowns"). Requires a real quote (yes_bid or yes_ask
    actually present) -- an unquoted market isn't a real price. Used by
    both best5_touchdowns (ranking) and build.py's touchdown-prediction
    freezing (grading), so the two never disagree about what price was used.
    """
    for m in kalshi_td_props:
        title = m.get("market_title") or ""
        if ": 1+" not in title:
            continue
        if m.get("yes_bid") is None and m.get("yes_ask") is None:
            continue
        if not title.split(":")[0].strip() == player_name:
            continue
        if m.get("price") is not None:
            return m["price"]
    return None


def best5_touchdowns(td_rows: list[dict], kalshi_td_props: list[dict]) -> list[dict]:
    """
    Converts each player's next-game projected total TDs into a Poisson
    probability of 1+ TD, then compares against Kalshi's real "1+
    touchdowns" market price for that same player where one exists
    (matched by name substring in the market title -- Kalshi's touchdown
    market titles are literally "{Player Name}: N+ touchdowns").

    Only players with a real Kalshi quote (yes_bid or yes_ask actually
    present, not null) are eligible -- same "edgeEligible" principle as
    the MLB site: a market with no real quote isn't informed disagreement,
    it's just an empty order book, and ranking by edge against it would
    be ranking by noise, not signal.
    """
    candidates = []
    for r in td_rows:
        next_game = r.get("next_game")
        if not next_game or not next_game.get("projected_total"):
            continue
        market_price = find_kalshi_1plus_td_price(kalshi_td_props, r["player"])
        if market_price is None:
            continue
        model_prob = poisson_prob_at_least(1, next_game["projected_total"])
        edge = round(model_prob - market_price, 3)
        candidates.append({
            "player": r["player"],
            "team_badge": r.get("team_badge"),
            "opponent": next_game["opponent"],
            "week": next_game["week"],
            "model_prob": round(model_prob * 100, 1),
            "market_prob": round(market_price * 100, 1),
            "edge": round(edge * 100, 1),
            "call": "OVER" if edge > 0 else "UNDER",
        })
    candidates.sort(key=lambda c: abs(c["edge"]), reverse=True)
    return candidates[:5]
