"""
Real FanDuel lines (moneylines + player props) via The Odds API
(the-odds-api.com) -- a licensed data aggregator, NOT a scraper.

WHY THIS IS BUILT AROUND A CREDIT BUDGET
The free plan is 500 credits/month. A call costs (markets x regions),
where `bookmakers=fanduel` counts as one region-equivalent. Two very
different cost profiles:

  - Moneylines: ONE bulk call for the whole slate = 1 credit. Cheap
    enough to refresh daily.
  - Player props: no bulk endpoint exists -- each game needs its own
    /events/{id}/odds call. 4 markets x ~14 games is roughly 56 credits
    per full pull, so props are pulled about once a week.

Guards that follow from that:
  * The /events list (game schedule) is free and is used to skip games
    that have already started or are more than a week out.
  * Before the per-game loop, the remaining budget reported by the API's
    own x-requests-remaining header is checked. If the full slate would
    dip below RESERVE_CREDITS, fewer games are pulled (soonest first)
    instead of running the month dry.
  * A failed or empty pull never overwrites the last good cached data
    (that decision lives in build.py).

SECRETS
The key is read from the ODDS_API_KEY environment variable (a GitHub
Actions secret). The Odds API only accepts the key as a query parameter,
so `requests` error messages contain it. Everything this module returns
or logs goes through _clean(), because build.py writes results into
data/ and that folder is committed to the repo.

Everything here fails soft: errors are returned, never raised, and
nothing is ever fabricated.
"""

import json
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE_URL = "https://api.the-odds-api.com/v4"
SPORT = "americanfootball_nfl"
BOOKMAKER = "fanduel"

DATA_DIR = Path(__file__).parent.parent / "data"
USAGE_LOG_PATH = DATA_DIR / "oddsapi_usage_log.json"

# Real NFL player-prop market keys per The Odds API docs, mapped to this
# site's stat columns.
PROP_MARKETS = {
    "player_pass_yds": "passing_yards",
    "player_rush_yds": "rushing_yards",
    "player_reception_yds": "receiving_yards",
    "player_receptions": "receptions",
}
# Anytime-TD (market "player_anytime_td") is deliberately NOT requested:
# every extra market costs one more credit per game, and nothing on the
# site displays it yet. Flip on only together with a display for it.
CREDITS_PER_EVENT = len(PROP_MARKETS)

RESERVE_CREDITS = 60        # never plan a pull that would leave less than this
LOOKAHEAD_DAYS = 7          # only games starting within this window
REQUEST_TIMEOUT = 25

# The Odds API's exact team names, keyed by this site's abbreviations.
TEAM_NAMES = {
    "ARI": "Arizona Cardinals", "ATL": "Atlanta Falcons", "BAL": "Baltimore Ravens",
    "BUF": "Buffalo Bills", "CAR": "Carolina Panthers", "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals", "CLE": "Cleveland Browns", "DAL": "Dallas Cowboys",
    "DEN": "Denver Broncos", "DET": "Detroit Lions", "GB": "Green Bay Packers",
    "HOU": "Houston Texans", "IND": "Indianapolis Colts", "JAX": "Jacksonville Jaguars",
    "KC": "Kansas City Chiefs", "LA": "Los Angeles Rams", "LAC": "Los Angeles Chargers",
    "LV": "Las Vegas Raiders", "MIA": "Miami Dolphins", "MIN": "Minnesota Vikings",
    "NE": "New England Patriots", "NO": "New Orleans Saints", "NYG": "New York Giants",
    "NYJ": "New York Jets", "PHI": "Philadelphia Eagles", "PIT": "Pittsburgh Steelers",
    "SEA": "Seattle Seahawks", "SF": "San Francisco 49ers", "TB": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans", "WAS": "Washington Commanders",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _api_key() -> str | None:
    """The key from the ODDS_API_KEY secret, with stray whitespace/newlines
    and wrapping quotes removed (a common copy-paste artifact that makes an
    otherwise valid key come back as HTTP 401)."""
    raw = os.environ.get("ODDS_API_KEY") or ""
    key = raw.strip().strip("'\"").strip()
    return key or None


def _clean(text) -> str:
    """Strip the API key (literal value, and any apiKey=... query
    fragment) out of any string before it is returned, logged or saved."""
    msg = str(text)
    key = _api_key()
    if key:
        msg = msg.replace(key, "***")
    return re.sub(r"(?i)(api_?key=)[^&\s'\"]+", r"\1***", msg)


class OddsApiError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def _get(path: str, params: dict | None = None):
    """Returns (json, headers). Raises OddsApiError with a key-free
    message on any failure."""
    key = _api_key()
    if not key:
        raise OddsApiError("ODDS_API_KEY is not set")
    q = dict(params or {})
    q["apiKey"] = key
    try:
        resp = requests.get(f"{BASE_URL}{path}", params=q, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json(), resp.headers
    except Exception as e:  # noqa: BLE001 -- everything must be sanitised
        resp_obj = getattr(e, "response", None)
        status = getattr(resp_obj, "status_code", None)
        # The API explains WHY it refused (bad key vs. quota used up vs.
        # disabled key) in the response body -- surface that, or a 401 is
        # impossible to diagnose from the log.
        detail = ""
        if resp_obj is not None:
            try:
                body = resp_obj.json()
                if isinstance(body, dict):
                    bits = [str(body[k]) for k in ("error_code", "message") if body.get(k)]
                    if bits:
                        detail = " | API says: " + " - ".join(bits)
            except Exception:
                pass
        raise OddsApiError(
            _clean(f"{type(e).__name__}{f' (HTTP {status})' if status else ''}: {e}{detail}"), status) from None


# --------------------------------------------------------------------------
# Real credit usage, straight from the API's own response headers
# --------------------------------------------------------------------------

def _int_or_none(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _log_usage(label: str, headers) -> dict:
    snap = {
        "call": label,
        "requests_remaining": _int_or_none(headers.get("x-requests-remaining")),
        "requests_used": _int_or_none(headers.get("x-requests-used")),
        "requests_last": _int_or_none(headers.get("x-requests-last")),
        "logged_at": _now().strftime("%Y-%m-%d %H:%M UTC"),
    }
    try:
        log = json.loads(USAGE_LOG_PATH.read_text()) if USAGE_LOG_PATH.exists() else {"entries": []}
    except Exception:
        log = {"entries": []}
    log["entries"] = (log.get("entries", []) + [snap])[-200:]
    try:
        USAGE_LOG_PATH.write_text(json.dumps(log, indent=2))
    except Exception:
        pass
    return snap


def current_usage() -> dict | None:
    """Most recent real usage snapshot, or None if nothing logged yet."""
    try:
        log = json.loads(USAGE_LOG_PATH.read_text())
        entries = log.get("entries", [])
        return entries[-1] if entries else None
    except Exception:
        return None


# --------------------------------------------------------------------------
# Odds math
# --------------------------------------------------------------------------

def american_to_prob(price: float | int | None) -> float | None:
    """Raw implied probability of an American price (includes the vig)."""
    if price is None or price == 0:
        return None
    return 100 / (price + 100) if price > 0 else -price / (-price + 100)


def no_vig_pair(home_price, away_price) -> tuple[float | None, float | None]:
    """Implied win probabilities with the bookmaker's margin removed
    (each raw probability divided by their sum)."""
    ph, pa = american_to_prob(home_price), american_to_prob(away_price)
    if ph is None or pa is None:
        return None, None
    total = ph + pa
    return ph / total, pa / total


def fmt_price(price) -> str:
    if price is None:
        return "--"
    return f"+{int(price)}" if price > 0 else f"{int(price)}"


# --------------------------------------------------------------------------
# Player-name canonicalisation
# --------------------------------------------------------------------------

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def normalize_name(name: str) -> str:
    """Lowercase, accent-folded, punctuation-free, suffix-free key, so
    'Deebo Samuel' and 'Deebo Samuel Sr.' (or 'Amon-Ra St. Brown' and
    'Amon Ra St Brown') compare equal."""
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[.'’`]", "", s.lower())
    s = s.replace("-", " ")
    tokens = [t for t in s.split() if t]
    while tokens and tokens[-1] in _SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def canonicalize_props(props: dict, known_names) -> tuple[dict, dict]:
    """
    Re-key {player: {stat: line}} onto this site's own spelling of each
    name (nflreadpy's player_display_name), which is what the templates
    look lines up by. A name is only remapped when exactly ONE known
    player shares its normalised form; ambiguous or unknown names are
    left exactly as FanDuel spelled them and counted, never guessed.
    """
    by_norm: dict[str, set] = {}
    for n in known_names:
        by_norm.setdefault(normalize_name(n), set()).add(n)
    known = set(known_names)

    out: dict[str, dict] = {}
    stats = {"exact": 0, "remapped": 0, "ambiguous": 0, "unknown": 0}
    for name, stat_lines in props.items():
        if name in known:
            target = name
            stats["exact"] += 1
        else:
            cands = by_norm.get(normalize_name(name), set())
            if len(cands) == 1:
                target = next(iter(cands))
                stats["remapped"] += 1
            else:
                target = name
                stats["ambiguous" if len(cands) > 1 else "unknown"] += 1
        out.setdefault(target, {}).update(stat_lines)
    return out, stats


# --------------------------------------------------------------------------
# Moneylines -- one cheap bulk call
# --------------------------------------------------------------------------

def _parse_time(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def _book_params(book_mode: str) -> dict:
    return {"bookmakers": BOOKMAKER} if book_mode == "bookmakers" else {"regions": "us"}


def _distill_games(raw_games: list) -> list[dict]:
    """One real entry per game with whatever FanDuel markets it has --
    h2h, spreads, totals -- each parsed independently so a game missing
    one market (e.g. totals not posted yet) still keeps the others."""
    games = []
    for g in raw_games:
        home, away = g.get("home_team"), g.get("away_team")
        entry = {
            "id": g.get("id"), "home_team": home, "away_team": away,
            "commence_time": g.get("commence_time"),
            "home_price": None, "away_price": None, "h2h_update": None,
            "home_spread": None, "away_spread": None,
            "home_spread_price": None, "away_spread_price": None, "spread_update": None,
            "total_point": None, "over_price": None, "under_price": None, "total_update": None,
        }
        has_any = False
        for book in g.get("bookmakers", []):
            if book.get("key") != BOOKMAKER:
                continue
            for market in book.get("markets", []):
                key = market.get("key")
                if key == "h2h":
                    prices = {o.get("name"): o.get("price") for o in market.get("outcomes", [])}
                    entry["home_price"] = prices.get(home)
                    entry["away_price"] = prices.get(away)
                    entry["h2h_update"] = market.get("last_update") or book.get("last_update")
                    has_any = True
                elif key == "spreads":
                    pts = {o.get("name"): o.get("point") for o in market.get("outcomes", [])}
                    prices = {o.get("name"): o.get("price") for o in market.get("outcomes", [])}
                    entry["home_spread"] = pts.get(home)
                    entry["away_spread"] = pts.get(away)
                    entry["home_spread_price"] = prices.get(home)
                    entry["away_spread_price"] = prices.get(away)
                    entry["spread_update"] = market.get("last_update") or book.get("last_update")
                    has_any = True
                elif key == "totals":
                    for o in market.get("outcomes", []):
                        side = str(o.get("name", "")).lower()
                        if side == "over":
                            entry["total_point"] = o.get("point")
                            entry["over_price"] = o.get("price")
                            has_any = True
                        elif side == "under":
                            entry["under_price"] = o.get("price")
                            entry["total_update"] = market.get("last_update") or book.get("last_update")
                            has_any = True
        if has_any:
            games.append(entry)
    return games


def fetch_moneylines() -> dict:
    """
    Real FanDuel h2h prices for the upcoming slate: ONE bulk call, 1
    credit. Tries `bookmakers=fanduel` first (smaller response); if the
    API rejects that, falls back to `regions=us` and filters to FanDuel
    locally. `book_mode` records which worked, and is reused for the
    per-game prop calls.
    """
    result = {"games": [], "error": None, "pulled_at": _now().strftime("%Y-%m-%d %H:%M UTC"),
              "book_mode": None, "diagnostics": {}}
    raw = None
    last_err = None
    for mode in ("bookmakers", "regions"):
        try:
            raw, headers = _get(f"/sports/{SPORT}/odds",
                                {**_book_params(mode), "markets": "h2h,spreads,totals", "oddsFormat": "american"})
            result["book_mode"] = mode
            result["diagnostics"]["usage"] = _log_usage(f"moneylines[{mode}]", headers)
            break
        except OddsApiError as e:
            last_err = str(e)
            # Only a request-shape problem (e.g. 400/422) is worth retrying
            # in the other book mode. A missing/rejected key, exhausted
            # quota, rate limit or outage would fail identically.
            if e.status is None or e.status in (401, 403, 429) or e.status >= 500 or "ODDS_API_KEY" in last_err:
                break
    if raw is None:
        result["error"] = last_err
        return result

    result["games"] = _distill_games(raw)
    result["diagnostics"]["games_returned"] = len(raw)
    result["diagnostics"]["games_with_fanduel_line"] = len(result["games"])
    return result


# --------------------------------------------------------------------------
# Player props -- one call per game, budget-guarded
# --------------------------------------------------------------------------

def fetch_player_props(book_mode: str = "bookmakers", remaining_credits: int | None = None) -> dict:
    """
    Real FanDuel player props. Returns {"lines": [...], "error", ...} where
    every line is {player, stat, line, over_price, under_price, event_id,
    commence_time} -- tagged with its game so build.py can drop a line the
    moment its game has started (a stale cached line must never be shown
    against next week's game).

    remaining_credits: real remaining budget from the moneyline call. When
    known, the per-game loop is capped so the month keeps RESERVE_CREDITS.
    """
    result = {"lines": [], "error": None, "pulled_at": _now().strftime("%Y-%m-%d %H:%M UTC"),
              "diagnostics": {}}
    diag = result["diagnostics"]

    try:
        events, headers = _get(f"/sports/{SPORT}/events", {})
        _log_usage("events (free)", headers)
    except OddsApiError as e:
        result["error"] = str(e)
        return result

    now = _now()
    horizon = now + timedelta(days=LOOKAHEAD_DAYS)
    upcoming = []
    for ev in events:
        t = _parse_time(ev.get("commence_time"))
        if t and now < t <= horizon:
            upcoming.append((t, ev))
    upcoming.sort(key=lambda x: x[0])
    diag["events_listed"] = len(events)
    diag["events_in_window"] = len(upcoming)

    if remaining_credits is not None:
        affordable = max(0, (remaining_credits - RESERVE_CREDITS) // CREDITS_PER_EVENT)
        diag["credits_remaining_before"] = remaining_credits
        if affordable < len(upcoming):
            diag["budget_limited"] = True
            upcoming = upcoming[:affordable]
    else:
        diag["credits_remaining_before"] = None
    diag["events_attempted"] = len(upcoming)

    markets = ",".join(PROP_MARKETS)
    event_errors = []
    events_with_lines = 0
    for t, ev in upcoming:
        try:
            data, headers = _get(f"/sports/{SPORT}/events/{ev['id']}/odds",
                                 {**_book_params(book_mode), "markets": markets, "oddsFormat": "american"})
            diag["usage"] = _log_usage(f"event-odds:{ev['id']}", headers)
        except OddsApiError as e:
            event_errors.append(str(e))
            continue

        got = 0
        for book in data.get("bookmakers", []):
            if book.get("key") != BOOKMAKER:
                continue
            for market in book.get("markets", []):
                stat = PROP_MARKETS.get(market.get("key"))
                if not stat:
                    continue
                by_player: dict[str, dict] = {}
                for o in market.get("outcomes", []):
                    player, point = o.get("description"), o.get("point")
                    if not player or point is None:
                        continue
                    slot = by_player.setdefault(player, {"line": point})
                    side = str(o.get("name", "")).lower()
                    if side in ("over", "under"):
                        slot[f"{side}_price"] = o.get("price")
                for player, slot in by_player.items():
                    result["lines"].append({
                        "player": player, "stat": stat, "line": slot["line"],
                        "over_price": slot.get("over_price"), "under_price": slot.get("under_price"),
                        "event_id": ev["id"], "commence_time": ev.get("commence_time"),
                    })
                    got += 1
        if got:
            events_with_lines += 1

    diag["events_with_fanduel_lines"] = events_with_lines
    diag["events_failed"] = len(event_errors)
    if event_errors:
        diag["sample_event_errors"] = sorted(set(event_errors))[:3]
    diag["lines_found"] = len(result["lines"])
    if upcoming and len(event_errors) == len(upcoming):
        result["error"] = f"all {len(upcoming)} per-game calls failed: {event_errors[0]}"
    return result


# --------------------------------------------------------------------------
# Helpers for build.py -- turn cached raw results into what pages need
# --------------------------------------------------------------------------

def upcoming_only(items: list[dict], now: datetime | None = None) -> list[dict]:
    """Drop anything whose game has already started."""
    now = now or _now()
    out = []
    for it in items:
        t = _parse_time(it.get("commence_time"))
        if t is None or t > now:
            out.append(it)
    return out


def lines_to_props(lines: list[dict]) -> dict:
    """[{player, stat, line,...}] -> {player: {stat: line}} (same shape as
    the PrizePicks props dict, so templates can treat them alike)."""
    props: dict[str, dict] = {}
    for ln in lines:
        props.setdefault(ln["player"], {})[ln["stat"]] = ln["line"]
    return props


def lines_to_prices(lines: list[dict]) -> dict:
    """
    [{player, stat, line, over_price, under_price,...}] -> {player:
    {stat: {"over_price_str", "under_price_str"}}} -- the real American
    odds FanDuel actually prices this line at (e.g. -115/-105), kept
    separate from lines_to_props() (which only carries the line itself)
    so existing scalar-line template code doesn't need to change shape.
    Formatted with fmt_price so a page just drops these strings in
    directly.
    """
    prices: dict[str, dict] = {}
    for ln in lines:
        prices.setdefault(ln["player"], {})[ln["stat"]] = {
            "over_price_str": fmt_price(ln.get("over_price")),
            "under_price_str": fmt_price(ln.get("under_price")),
        }
    return prices


def fmt_point(pt) -> str:
    if pt is None:
        return "--"
    return f"+{pt:g}" if pt > 0 else f"{pt:g}"


def odds_for_game(games: list[dict], home_abbr: str, away_abbr: str) -> dict | None:
    """
    Real FanDuel moneyline, spread and total for one matchup, matched on
    exact team names (never substrings). Each of the three sub-dicts is
    None independently when FanDuel hasn't posted that specific market
    yet, so e.g. a game with a moneyline but no total posted still shows
    the moneyline. None (the whole thing) only when FanDuel has nothing
    at all for this game.
    """
    home_name, away_name = TEAM_NAMES.get(home_abbr), TEAM_NAMES.get(away_abbr)
    if not home_name or not away_name:
        return None
    g = next((g for g in games if g.get("home_team") == home_name and g.get("away_team") == away_name), None)
    if g is None:
        return None

    moneyline = None
    if g.get("home_price") is not None and g.get("away_price") is not None:
        p_home, p_away = no_vig_pair(g["home_price"], g["away_price"])
        moneyline = {
            "home_price": g["home_price"], "away_price": g["away_price"],
            "home_prob": round(p_home * 100, 1), "away_prob": round(p_away * 100, 1),
            "home_price_str": fmt_price(g["home_price"]), "away_price_str": fmt_price(g["away_price"]),
            "last_update": g.get("h2h_update"),
        }

    spread = None
    if g.get("home_spread") is not None and g.get("away_spread") is not None:
        p_home, p_away = no_vig_pair(g.get("home_spread_price"), g.get("away_spread_price"))
        spread = {
            "home_point": g["home_spread"], "away_point": g["away_spread"],
            "home_point_str": fmt_point(g["home_spread"]), "away_point_str": fmt_point(g["away_spread"]),
            "home_price_str": fmt_price(g.get("home_spread_price")), "away_price_str": fmt_price(g.get("away_spread_price")),
            "home_prob": round(p_home * 100, 1) if p_home is not None else None,
            "away_prob": round(p_away * 100, 1) if p_away is not None else None,
            "last_update": g.get("spread_update"),
        }

    total = None
    if g.get("total_point") is not None:
        p_over, p_under = no_vig_pair(g.get("over_price"), g.get("under_price"))
        total = {
            "point": g["total_point"],
            "over_price_str": fmt_price(g.get("over_price")), "under_price_str": fmt_price(g.get("under_price")),
            "over_prob": round(p_over * 100, 1) if p_over is not None else None,
            "under_prob": round(p_under * 100, 1) if p_under is not None else None,
            "last_update": g.get("total_update"),
        }

    if moneyline is None and spread is None and total is None:
        return None
    return {"moneyline": moneyline, "spread": spread, "total": total}


def moneyline_for_game(games: list[dict], home_abbr: str, away_abbr: str) -> dict | None:
    """Back-compat shim: the old moneyline-only shape, built from
    odds_for_game(). Kept in case anything still calls this directly."""
    full = odds_for_game(games, home_abbr, away_abbr)
    return full["moneyline"] if full else None
