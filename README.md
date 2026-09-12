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
1. Pulls a live snapshot via `nflreadpy` (player stats + injuries).
2. Computes provisional, sample-size-only tiers (see `scripts/dataio.py`
   docstring — there's no calibrated model yet, and this build doesn't
   pretend there is one).
3. Renders `docs/index.html`, `docs/passing.html`, `docs/receiving.html`,
   `docs/history.html` from the Jinja templates in `scripts/templates/`.

`.github/workflows/build.yml` runs that script on a schedule, then
commits the regenerated `docs/` and `data/` folders back to the repo if
anything changed. GitHub Pages picks up the new commit automatically.

## Local preview (optional)

You don't need this to make the site work, but if you want to see a
build before pushing:
```bash
pip install -r requirements.txt
python scripts/build.py          # live fetch + rebuild
open docs/index.html             # or just double-click it
```
`docs/index.html` is a real static file — no server needed, unlike the
first version of this project.

## Known gaps, stated plainly (same spirit as the MLB build)

- **No Kalshi / PrizePicks market data.** The MLB site pulls live Kalshi
  prices; this doesn't yet. GitHub Actions runners have full internet
  access (unlike the sandbox this was built in), so wiring in a real
  market source is realistic here — it just isn't done yet.
- **No calibrated confidence/edge model.** `MIN_SAMPLE`/`DAMPEN` for
  NFL's weekly cadence is an open design question in the project brief.
  The "tier" shown is sample-size only, not a tuned prediction.
- **No Touchdown, Rushing, Receptions, Kicking, or Teams/Matchups pages**
  yet — listed honestly as "coming soon" in the footer.
- **No date picker** (the MLB site lets you browse past days). This
  build only ever shows the latest snapshot; historical snapshots would
  need to be saved per-run rather than overwritten.
- **No auto-grading of past predictions**, since there are no predictions
  yet to grade — History page says so directly instead of faking a
  record.

## Suggested next session

Same priority order as the original project brief:
1. Decide real `MIN_SAMPLE`/`DAMPEN` values for NFL's weekly cadence.
2. Add a real market-odds source (Kalshi/PrizePicks) inside
   `scripts/build.py` — Actions runners can reach those APIs.
3. Save each week's build as a dated snapshot instead of overwriting, to
   enable a History date picker like the MLB site's.
4. Build Teams/Matchups using `load_player_stats` filtered to past
   meetings vs. the upcoming opponent.
