#!/usr/bin/env python3
"""
tsc-watch — collector.
Fetches TensorCash (TSC) data from the tscscan.xyz explorer and the SafeTrade
exchange and stores it in a local SQLite database (data/tsc.db). Standard library only.

Usage:
  python3 collect.py                 # incremental (new blocks, prices, snapshot)
  python3 collect.py --backfill      # full history (blocks since genesis, prices since listing)
  python3 collect.py --fixtures DIR  # offline test mode: reads JSON samples from DIR
"""
import json
import os
import sqlite3
import sys
import time
import urllib.request
import urllib.parse
import urllib.error

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "tsc.db")

EXPLORER = "https://tscscan.xyz"
SAFETRADE = "https://safetrade.com/api/v2/trade/public"
MARKET = "tscusdt"
LISTING_TS = 1786900000  # ~17 Aug 2026, first daily candle on SafeTrade
UA = "tsc-watch/1.0 (+https://tsc.watch)"
SATS = 100_000_000

FIXTURES = None  # directory with JSON samples (test mode)


# ---------------------------------------------------------------- HTTP

def http_json(url, retries=3, timeout=40):
    """GET → JSON with simple retry. In fixtures mode reads a file named after the endpoint."""
    if FIXTURES:
        return _fixture(url)
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET {url} failed: {last}")


def _fixture(url):
    p = urllib.parse.urlparse(url)
    q = urllib.parse.parse_qs(p.query)
    path = p.path
    if path.startswith("/api/blocks"):
        page = int(q.get("page", ["1"])[0])
        f = os.path.join(FIXTURES, f"blocks_p{page}.json")
        if not os.path.exists(f):
            return {"items": [], "pagination": {"has_next": False, "page": page, "total_pages": page}}
        return json.load(open(f))
    if path.startswith("/api/home"):
        return json.load(open(os.path.join(FIXTURES, "home.json")))
    if path.startswith("/api/market-summary"):
        return json.load(open(os.path.join(FIXTURES, "market_summary.json")))
    if path.startswith("/api/analytics/mining-history"):
        return json.load(open(os.path.join(FIXTURES, "mining_history.json")))
    if path.startswith("/api/analytics"):
        return json.load(open(os.path.join(FIXTURES, "analytics30d.json")))
    if path.startswith("/api/address-aliases"):
        return json.load(open(os.path.join(FIXTURES, "aliases.json")))
    if path.startswith("/api/holders"):
        return json.load(open(os.path.join(FIXTURES, "holders.json")))
    if "k-line" in path:
        period = q.get("period", ["60"])[0]
        f = os.path.join(FIXTURES, f"kline_{period}.json")
        return json.load(open(f)) if os.path.exists(f) else []
    if path.startswith("/api/block/"):
        return {"items": [], "block": None}
    raise RuntimeError("no fixture for " + url)


# ---------------------------------------------------------------- DB

SCHEMA = """
CREATE TABLE IF NOT EXISTS blocks (
  height INTEGER PRIMARY KEY,
  timestamp INTEGER NOT NULL,
  miner TEXT,
  difficulty REAL,           -- effective_difficulty (after the proof-of-inference multiplier)
  base_difficulty REAL,      -- base difficulty from bits
  multiplier REAL,           -- effective_multiplier
  core_norm_difficulty REAL, -- core_normalized_difficulty
  tx_count INTEGER,
  size INTEGER,
  hash TEXT
);
CREATE INDEX IF NOT EXISTS blocks_ts ON blocks(timestamp);
CREATE INDEX IF NOT EXISTS blocks_miner ON blocks(miner);

CREATE TABLE IF NOT EXISTS prices (
  period INTEGER NOT NULL,   -- candle minutes: 60 or 1440
  ts INTEGER NOT NULL,       -- candle start (unix, UTC)
  open REAL, high REAL, low REAL, close REAL,
  volume REAL,               -- in TSC
  PRIMARY KEY (period, ts)
);

CREATE TABLE IF NOT EXISTS snapshots (
  ts INTEGER PRIMARY KEY,    -- fetch time
  height INTEGER,
  tip_ts INTEGER,
  price_usdt REAL,
  change_24h REAL,
  volume_24h_tsc REAL,
  volume_24h_usdt REAL,
  circ_supply REAL,          -- in TSC
  total_supply REAL,
  market_cap REAL,
  fdv REAL,
  holders INTEGER,
  mempool INTEGER,
  work_rate_24h REAL,        -- per explorer
  work_rate_50 REAL,
  difficulty REAL,
  block_reward REAL,         -- in TSC
  next_reward_height INTEGER,
  top10_pct REAL,
  top100_pct REAL,
  raw TEXT
);

CREATE TABLE IF NOT EXISTS daily_activity (
  day INTEGER PRIMARY KEY,   -- start of UTC day
  blocks INTEGER, transactions INTEGER, transfers INTEGER,
  new_addresses INTEGER, fees_sats INTEGER, gross_output_sats INTEGER,
  updated_ts INTEGER
);

CREATE TABLE IF NOT EXISTS holders_snapshots (
  ts INTEGER NOT NULL,
  rank INTEGER NOT NULL,
  address TEXT,
  balance REAL,              -- TSC
  net_flow_7d REAL,
  net_flow_30d REAL,
  supply_share REAL,
  tx_count INTEGER,
  first_seen_height INTEGER,
  last_seen_ts INTEGER,
  PRIMARY KEY (ts, rank)
);

CREATE TABLE IF NOT EXISTS aliases (
  address TEXT PRIMARY KEY,
  alias TEXT,
  source TEXT,
  updated_ts INTEGER
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);
"""


def db():
    os.makedirs(DATA_DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def meta_get(con, key, default=None):
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def meta_set(con, key, value):
    con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, str(value)))


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


# ---------------------------------------------------------------- blocks

def upsert_blocks(con, items):
    rows = []
    for b in items:
        rows.append((
            b["height"], b["timestamp"], b.get("miner_address"),
            float(b.get("effective_difficulty") or b.get("difficulty") or 0),
            float(b.get("base_difficulty") or b.get("difficulty") or 0),
            float(b.get("effective_multiplier") or 1.0),
            float(b.get("core_normalized_difficulty") or 0),
            b.get("tx_count"), b.get("size"), b.get("hash"),
        ))
    con.executemany("""INSERT OR REPLACE INTO blocks
        (height,timestamp,miner,difficulty,base_difficulty,multiplier,core_norm_difficulty,tx_count,size,hash)
        VALUES (?,?,?,?,?,?,?,?,?,?)""", rows)
    return len(rows)


def known_range(con):
    row = con.execute("SELECT MIN(height), MAX(height), COUNT(*) FROM blocks").fetchone()
    return row  # (min, max, count)


def sync_blocks(con, backfill=False, page_size=100, max_pages=None):
    """Page 1 = newest blocks. Walk down until we reach blocks we already have
    (incremental mode) or the end (backfill)."""
    _, max_known, count = known_range(con)
    max_known = max_known if max_known is not None else -1
    page = 1
    fetched = 0
    while True:
        data = http_json(f"{EXPLORER}/api/blocks?page={page}&page_size={page_size}")
        items = data.get("items") or []
        if not items:
            break
        fetched += upsert_blocks(con, items)
        con.commit()
        min_h = min(b["height"] for b in items)
        pag = data.get("pagination") or {}
        if page % 10 == 0 or not pag.get("has_next"):
            log(f"  blocks: page {page}/{pag.get('total_pages', '?')}, down to height {min_h}")
        if not backfill and min_h <= max_known:
            break
        if not pag.get("has_next"):
            break
        if max_pages and page >= max_pages:
            break
        page += 1
        time.sleep(0.15)
    fill_gaps(con)
    return fetched


def fill_gaps(con, limit=200):
    """Blocks can shift between pages — fetch missing heights one by one."""
    mn, mx, cnt = known_range(con)
    if mn is None or cnt == mx - mn + 1:
        return 0
    have = set(r[0] for r in con.execute("SELECT height FROM blocks"))
    missing = [h for h in range(mn, mx + 1) if h not in have]
    log(f"  block gaps: {len(missing)} (fetching up to {limit})")
    got = 0
    for h in missing[:limit]:
        try:
            d = http_json(f"{EXPLORER}/api/block/{h}?page=1&page_size=1")
            blk = d.get("block") or d
            if blk and blk.get("height") == h:
                upsert_blocks(con, [blk])
                got += 1
        except Exception as e:
            log(f"  block {h}: {e}")
        time.sleep(0.1)
    con.commit()
    return got


# ---------------------------------------------------------------- prices (SafeTrade)

def sync_prices(con, period, since_ts, limit=1000):
    """SafeTrade (Peatio) OHLCV candles: [ts, open, high, low, close, volume]."""
    ts_from = since_ts
    total = 0
    now = int(time.time())
    while ts_from < now:
        url = (f"{SAFETRADE}/markets/{MARKET}/k-line?period={period}"
               f"&time_from={ts_from}&limit={limit}")
        rows = http_json(url)
        if not isinstance(rows, list) or not rows:
            break
        con.executemany("""INSERT OR REPLACE INTO prices(period,ts,open,high,low,close,volume)
                           VALUES (?,?,?,?,?,?,?)""",
                        [(period, int(r[0]), float(r[1]), float(r[2]), float(r[3]),
                          float(r[4]), float(r[5])) for r in rows])
        con.commit()
        total += len(rows)
        last_ts = int(rows[-1][0])
        if len(rows) < limit or last_ts <= ts_from:
            break
        ts_from = last_ts + period * 60
        time.sleep(0.2)
    return total


def sync_all_prices(con, backfill=False):
    for period in (60, 1440):
        row = con.execute("SELECT MAX(ts) FROM prices WHERE period=?", (period,)).fetchone()
        last = row[0]
        if backfill or last is None:
            since = LISTING_TS
        else:
            since = last - period * 60 * 3  # re-fetch the last 3 candles (the current one keeps changing)
        n = sync_prices(con, period, since)
        log(f"  prices {period}m: {n} candles")


# ---------------------------------------------------------------- snapshot + analytics

def take_snapshot(con):
    home = http_json(f"{EXPLORER}/api/home?range=2h")
    ms = http_json(f"{EXPLORER}/api/market-summary")
    an = http_json(f"{EXPLORER}/api/analytics?range=30d")
    m = home.get("metrics", {})
    sup = an.get("supply", {})
    conc = sup.get("concentration", {})
    now = int(time.time())

    def f(x):
        try:
            return float(str(x).replace("%", ""))
        except (TypeError, ValueError):
            return None

    circ = (ms.get("circulating_supply_sats") or m.get("supply_sats") or 0) / SATS
    total = (ms.get("total_supply_sats") or sup.get("maximum_sats") or 0) / SATS
    con.execute("""INSERT OR REPLACE INTO snapshots VALUES
        (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        now, m.get("height"), m.get("tip_timestamp"),
        f(ms.get("price_usdt")), f(ms.get("price_change_percent_24h")),
        f(ms.get("volume_24h_tsc")), f(ms.get("volume_24h_usdt")),
        circ, total, f(ms.get("circulating_market_cap_usdt")), f(ms.get("total_market_cap_usdt")),
        m.get("holders") or sup.get("funded_addresses"), m.get("mempool_transactions"),
        m.get("network_work_rate_24h"), m.get("network_work_rate_50"),
        m.get("difficulty"), (m.get("current_block_reward_sats") or 0) / SATS,
        m.get("next_reward_height"),
        conc.get("top_10_percent"), conc.get("top_100_percent"),
        json.dumps({"metrics": m, "market": {k: v for k, v in ms.items() if k != "status"},
                    "supply": sup, "insights": an.get("insights")}),
    ))
    # daily activity (transactions, new addresses...) — overwrite the last 30 days
    for d in an.get("activity") or []:
        con.execute("""INSERT OR REPLACE INTO daily_activity VALUES (?,?,?,?,?,?,?,?)""", (
            d["timestamp"], d.get("blocks"), d.get("transactions"), d.get("transfers"),
            d.get("new_addresses"), d.get("fees_sats"), d.get("gross_output_sats"), now))
    # pool aliases
    try:
        al = http_json(f"{EXPLORER}/api/address-aliases").get("aliases") or {}
        for addr, name in al.items():
            con.execute("INSERT OR REPLACE INTO aliases VALUES (?,?,?,?)", (addr, name, "tscscan", now))
    except Exception as e:
        log(f"  aliases: {e}")
    con.commit()
    return now, m.get("height")


def sync_holders(con, ts, pages=4):
    """Top 100 addresses (4 pages × 25)."""
    n = 0
    for p in range(1, pages + 1):
        d = http_json(f"{EXPLORER}/api/holders?page={p}&page_size=25")
        for h in d.get("items") or []:
            con.execute("""INSERT OR REPLACE INTO holders_snapshots VALUES (?,?,?,?,?,?,?,?,?,?)""", (
                ts, h.get("rank"), h.get("address"), (h.get("balance_sats") or 0) / SATS,
                (h.get("net_flow_7d_sats") or 0) / SATS if h.get("net_flow_7d_sats") is not None else None,
                (h.get("net_flow_30d_sats") or 0) / SATS if h.get("net_flow_30d_sats") is not None else None,
                h.get("supply_share"), h.get("tx_count"), h.get("first_seen_height"),
                h.get("last_seen_timestamp")))
            n += 1
        time.sleep(0.15)
    con.commit()
    return n


# ---------------------------------------------------------------- state kept in the repo (state.json)

STATE_PATH = os.path.join(DATA_DIR, "state.json")
STATE_TABLES = {
    "snapshots": ("ts", None),        # raw column skipped (large)
    "daily_activity": ("day", None),
    "holders_snapshots": ("ts,rank", None),
    "aliases": ("address", None),
}


def export_state(con):
    """Saves small tables (own history) to JSON — blocks and prices can be rebuilt from APIs, these cannot."""
    out = {"exported_at": int(time.time()), "tables": {}}
    for t in STATE_TABLES:
        cols = [c[1] for c in con.execute(f"PRAGMA table_info({t})") if c[1] != "raw"]
        rows = con.execute(f"SELECT {','.join(cols)} FROM {t}").fetchall()
        out["tables"][t] = {"cols": cols, "rows": rows}
    out["meta"] = dict(con.execute("SELECT key,value FROM meta").fetchall())
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    os.replace(tmp, STATE_PATH)


def import_state(con):
    """Loads state.json into a fresh database (e.g. a new CI runner)."""
    if not os.path.exists(STATE_PATH):
        return 0
    have = con.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    try:
        st = json.load(open(STATE_PATH))
    except (OSError, json.JSONDecodeError):
        return 0
    n = 0
    for t, spec in (st.get("tables") or {}).items():
        if t not in STATE_TABLES:
            continue
        cols = spec["cols"]
        con.executemany(f"INSERT OR IGNORE INTO {t} ({','.join(cols)}) VALUES ({','.join('?'*len(cols))})", spec["rows"])
        n += len(spec["rows"])
    for k, v in (st.get("meta") or {}).items():
        if k != "last_collect_ts":
            con.execute("INSERT OR IGNORE INTO meta(key,value) VALUES(?,?)", (k, v))
    con.commit()
    if have == 0:
        log(f"  loaded state.json ({n} rows)")
    return n


# ---------------------------------------------------------------- main

def main(argv):
    global FIXTURES
    backfill = "--backfill" in argv
    if "--fixtures" in argv:
        FIXTURES = argv[argv.index("--fixtures") + 1]
    con = db()
    t0 = time.time()
    log("start" + (" (backfill)" if backfill else ""))
    import_state(con)
    if known_range(con)[2] == 0:
        backfill = True
        log("  empty block table → backfill")

    try:
        n = sync_blocks(con, backfill=backfill)
        mn, mx, cnt = known_range(con)
        log(f"blocks: +{n}, stored {cnt} (heights {mn}–{mx})")
    except Exception as e:
        log(f"ERROR blocks: {e}")

    try:
        sync_all_prices(con, backfill=backfill)
    except Exception as e:
        log(f"ERROR prices: {e}")

    try:
        ts, h = take_snapshot(con)
        log(f"snapshot @ block {h}")
        # holders: every ~6h is enough
        last_h = int(meta_get(con, "holders_last_ts", 0))
        if backfill or ts - last_h >= 6 * 3600:
            k = sync_holders(con, ts)
            meta_set(con, "holders_last_ts", ts)
            log(f"holders: {k} addresses")
    except Exception as e:
        log(f"ERROR snapshot: {e}")

    meta_set(con, "last_collect_ts", int(time.time()))
    con.commit()
    export_state(con)
    con.close()
    log(f"done ({time.time() - t0:.1f}s)")


if __name__ == "__main__":
    main(sys.argv[1:])
