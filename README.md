[README.md](https://github.com/user-attachments/files/32139446/README.md)
# NFL Prop Edge Board — static site (GitHub Pages + Actions)

This is built the same way as the MLB reference site: a scheduled
GitHub Actions job pulls real data, rebuilds static HTML, and pushes it
back to the repo. GitHub Pages just serves whatever's in `docs/`. Nobody
runs a server, and nobody needs to open a terminal for it to stay current.

## One-time setup (~5 minutes)

1. **Create a new GitHub repo** (public, so Pages is free) and push this
   folder to it:
   ```bash
   cd nfl_static
   git init
   git add .
   git commit -m "Initial NFL prop board"
   git branch -M main
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```

2. **Turn on GitHub Pages**: repo Settings → Pages → Source: "Deploy from
   a branch" → Branch: `main`, folder: `/docs` → Save.
   Your site will be live at `https://<you>.github.io/<repo>/` within a
   minute or two.

3. **Confirm Actions is enabled**: repo Settings → Actions → General →
   make sure "Allow all actions" is selected. The workflow in
   `.github/workflows/build.yml` will then run on its schedule
   automatically — no further setup needed.

4. Optional: trigger the first automated rebuild manually to confirm it
   works — Actions tab → "Rebuild NFL Prop Edge Board" → "Run workflow".

That's it. From here it runs itself, the same way the MLB site does.

## What the auto-build actually does

`scripts/build.py`:
1. Pulls a live snapshot via `nflreadpy` (player stats, injuries, schedule;
   historical 2023-2025 stats are pulled once and cached, since they don't
   change).
2. Computes provisional, sample-size-only tiers (see `scripts/dataio.py`
   docstring for the full reasoning) — there's a first-cut, reasoned
   `MIN_SAMPLE=30` / `DAMPEN=0.20` proposal for NFL's weekly cadence now,
   but it's not yet validated against real graded picks.
3. Renders all 9 pages from the Jinja templates in `scripts/templates/`:
   Home, Matchups, Passing, Receiving, Receptions, Rushing, Kicking,
   Touchdowns, History.

`.github/workflows/build.yml` runs that script on a schedule, then
commits the regenerated `docs/` and `data/` folders back to the repo if
anything changed. GitHub Pages picks up the new commit automatically.

## Kalshi integration (touchdown + game props only)

`scripts/kalshi_client.py` hits Kalshi's real, documented, unauthenticated
public API (`https://api.elections.kalshi.com/trade-api/v2`), scoped to
two prop categories, using the confirmed-real series tickers:

- **`KXNFLTD`** — anytime-touchdown markets (Touchdowns page)
- **`KXNFLGAME`** — game-level moneyline markets (Matchups page), useful
  context for touchdown correlation since a high implied game total
  means more scoring opportunities league-wide

Player yardage props (passing/rushing/receiving) are deliberately left
to PrizePicks instead — see below.

**This was live-verified, not just written against docs.** The first
attempt scanned Kalshi's `/events` endpoint broadly and found 0 matches
— not because the API failed, but because sports markets don't surface
in a broad scan; Kalshi's own guidance says sports/series-based
categories must be queried directly via `/events?series_ticker=X`. Two
candidate tickers were tried per bucket; `KXNFLTD` and `KXNFLGAME`
confirmed real (632 and 60 live markets respectively on the day this was
tested), while `FOOTBALLTOUCHDOWN` and `KXNFL` were tried and confirmed
NOT to exist, so they were dropped rather than left in as dead weight.
Each bucket is sorted by trading volume and capped at 40 markets for
display; the raw response is saved to `data/kalshi_raw.json`.

Still fails soft by design: if Kalshi's API changes shape or a request
fails, the site builds anyway and shows a clear "Kalshi fetch failed"
notice with the real error, never a fabricated number.

## PrizePicks integration -- not yet built, here's why

PrizePicks has **no official public API**, unlike Kalshi. Every existing
integration for it (a few open-source projects, several paid scrapers)
hits undocumented internal endpoints, which are commonly blocked by
anti-bot protection, especially from cloud/CI IP ranges like GitHub
Actions runners. That's a meaningfully different risk profile than
Kalshi's real documented API. This is planned for the next round, built
with the same fail-soft pattern, but flagged as experimental from day
one rather than presented as equally reliable.

## Kicking -- removed

Field goal attempts depend too heavily on decisions (coach's 4th-down
call, weather, game script) for this build to have an honest way to
project them. Rather than publish a number with no real basis, the page
was removed entirely -- `scripts/build.py` also cleans up the old
`docs/kicking.html` file so it doesn't linger as an orphaned URL.

## Page-by-page notes

- **Matchups** — real upcoming games from the live schedule, now with a
  real dampened projection per player/stat (see "Projection engine"
  below), plus each team's actual historical performance against that
  specific opponent (2023–2025). Two teams with no recent meetings show
  "no meetings on record," never a guessed number.
- **Receiving vs. Receptions** — kept as two separate pages since they're
  two separate, separately-priced props on PrizePicks and Kalshi.
- **Touchdowns** — combines passing/rushing/receiving TDs into one
  cross-position board, since TD props aren't naturally owned by one
  stat page.
- **Kicking** — includes a 50+ yard attempt count pulled from the
  distance-bucketed FG data, not just makes/attempts totals.
- **History** — reports how many projections have been frozen and
  logged (real, growing number), and is explicit that grading doesn't
  exist yet because there's no market line to define a hit/miss against.

## Projection engine

`dataio.project_stat(player, stat)` implements the brief's "dampen every
correction" principle as real code, not just a policy note:

```
baseline   = player's own 2023-2025 career average for this stat
             (falls back to the league-wide position average for
             players with no career history, e.g. rookies)
observed   = player's 2026-season-to-date average for this stat
projected  = baseline + DAMPEN * (observed - baseline)
```

With `DAMPEN = 0.20`, a single game's result only ever nudges a
player's baseline 20% of the way toward that game's number — never a
full override from one data point, per the brief. Every projection is
frozen at build time (`FROZEN_AT`, set once per build run) and appended
to `data/projections_log.csv`, so there's a permanent, timestamped
record even before any grading logic exists to check it against.

This is intentionally *not* yet an "edge" calculation — edge requires a
market line to compare against, which isn't wired in. What exists now
is a real, reasoned projection; comparing it to a market price is the
natural next step once Kalshi/PrizePicks data is added.

## Local preview (optional)

You don't need this to make the site work, but if you want to see a
build before pushing:
```bash
pip install -r requirements.txt
python scripts/build.py          # live fetch + rebuild
open docs/index.html             # or just double-click it
```
`docs/index.html` is a real static file — no server needed.

## Known gaps, stated plainly (same spirit as the MLB build)

- **No Kalshi / PrizePicks market data.** The MLB site pulls live Kalshi
  prices; this doesn't yet. GitHub Actions runners have full internet
  access (unlike the sandbox this was built in), so wiring in a real
  market source is realistic here — it just isn't done yet.
- **No calibrated confidence/edge model yet.** `MIN_SAMPLE=30` /
  `DAMPEN=0.20` now power a real projection engine (see above), but
  there's still no "edge" — that requires a market line to compare the
  projection against, and no market source is wired in.
- **No grading yet.** Projections are frozen and logged, but nothing
  calls one a hit or a miss yet — needs either a market line or a
  decided actual-vs-projected error metric.
- **No date picker** (the MLB site lets you browse past days). This
  build only ever shows the latest snapshot; historical snapshots would
  need to be saved per-run rather than overwritten.
- **No auto-grading of past predictions**, since there are no predictions
  yet to grade — History page says so directly instead of faking a
  record.
- **No weather factor for passing.** Needs its own wind-specific
  derivation per the brief, not a ported MLB formula — not built yet.

## Suggested next session

1. Verify the Kalshi integration against a real Actions run and fix the
   discovery filters based on the actual response shape (see above).
2. Build the PrizePicks integration for passing/rushing/receiving/
   receptions props, flagged experimental, fail-soft like Kalshi.
3. Build the actual "reasoning" layer: combine the dampened projections,
   the correlation flags, and Kalshi's game-level context (spread/total)
   into a human-readable case for each pick, not just raw numbers.
4. Decide a grading rule (market line hit/miss, or a plain
   actual-vs-projected error threshold) and start populating History
   for real.
5. Save each week's build as a dated snapshot instead of overwriting, to
   enable a History date picker like the MLB site's.
6. Derive the wind-specific passing weather factor.
