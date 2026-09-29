# tsc.watch

Independent TensorCash (TSC) dashboard: SafeTrade market data, issuance, network work rate, mining pools, cost to mine, largest holders.
Data from the tscscan.xyz explorer and SafeTrade, refreshed by GitHub Actions and published with GitHub Pages at https://tsc.watch.

```
collect.py                    fetches blocks, prices and snapshots → data/tsc.db (SQLite, kept in the Actions cache)
build.py                      renders the static site (site/, inline SVG charts) and site/data.json
costs.py                      cost-to-mine model and its assumptions
data/state.json               the site's own history (snapshots, holders), committed by the workflow
.github/workflows/update.yml  collect → build → deploy
```

Run locally: `python3 collect.py && python3 build.py` (the first run backfills from genesis, a few minutes), then open `site/index.html`.
