#!/usr/bin/env python3
"""
tsc-watch — collector.
Fetches TensorCash (TSC) data from the tscscan.xyz explorer (data API at api.tscscan.xyz)
and the SafeTrade exchange and stores it in a local SQLite database (data/tsc.db).
Standard library only.

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

EXPLORER = "https://tscscan.xyz"         # explorer site: market summary
API = "https://api.tscscan.xyz/v1"       # explorer data API (mainnet)
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
        except urllib.error.HTTPError as e:
            last = e
            if 400 <= e.code < 500 and e.code != 429:   # a missing route or record will not appear on retry
                break
            time.sleep(1.5 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET {url} failed: {last}")


def _fixture(url):
    """Fixture files hold raw responses in the live API format."""
    p = urllib.parse.urlparse(url)
    q = urllib.parse.parse_qs(p.query)
    path = p.path
    page = int(q.get("page", ["1"])[0])
    empty = {"data": [], "page": {"limit": 100, "number": page, "total": 0, "total_pages": 0}}

    def load(name, default=None):
        f = os.path.join(FIXTURES, name)
        if os.path.exists(f):
            return json.load(open(f))
        if default is None:
            raise RuntimeError("no fixture for " + url)
        return default

    if path == "/v1/blocks":
        return load(f"blocks_p{page}.json", empty)
    if path.startswith("/v1/blocks/") and path.endswith("/proof"):
        return load(f"proof_{path.split('/')[3][:12]}.json", {"data": {}})
    if path.startswith("/v1/stats/") or path.startswith("/v1/mempool/"):
        return load(path[4:].replace("/", "_") + ".json")   # stats_chain.json, mempool_summary.json, ...
    if path == "/v1/addresses/aliases":
        return load("aliases.json")
    if path == "/v1/addresses":
        return load("holders.json")
    if path.startswith("/v1/addresses/"):
        return load(f"addr_{path.split('/')[3][:12]}_p{page}.json", empty)
    if path.startswith("/v1/transactions/"):
        return load(f"outputs_{path.split('/')[3][:12]}_p{page}.json", empty)
    if path.startswith("/api/market-summary"):
        return load("market_summary.json")
    if "k-line" in path:
        period = q.get("period", ["60"])[0]
        f = os.path.join(FIXTURES, f"kline_{period}.json")
        return json.load(open(f)) if os.path.exists(f) else []
    if "git.tensorcash.org" in url:
        f = os.path.join(FIXTURES, "releases.json")
        return json.load(open(f)) if os.path.exists(f) else []
    raise RuntimeError("no fixture for " + url)


# ---------------------------------------------------------------- explorer API (api.tscscan.xyz/v1)
# Responses are {"data": ..., "page": {"number", "total_pages", ...}} with amounts as
# {"precision": 8, "units": "<int string>"}. These helpers map them to the field names used below.

def api(path):
    return http_json(API + path)


def sats(amount):
    """{"precision": 8, "units": "123"} → 123; None when missing or unknown."""
    if not isinstance(amount, dict) or amount.get("units") is None:
        return None
    return int(amount["units"])


def has_next(page):
    return (page.get("number") or 0) < (page.get("total_pages") or 0)


def api_blocks(page, limit=100):
    """One page of the block list (newest first) → (items, page info)."""
    d = api(f"/blocks?limit={limit}&page={page}")
    items = []
    for b in d.get("data") or []:
        pr = b.get("proof") or {}
        price = pr.get("price_multiplier")           # proof-v4 price; None on older blocks
        mult = float(price or 1)
        eff = float(b.get("difficulty") or 0)       # effective difficulty, i.e. base × price
        items.append({
            "height": b["height"], "timestamp": b["time"], "miner_address": b.get("miner_address"),
            "effective_difficulty": eff, "base_difficulty": eff / mult, "effective_multiplier": mult,
            "tx_count": b.get("tx_count"), "size": b.get("size_bytes"), "hash": b.get("hash"),
            "proof": {"version": pr.get("version"), "multiplier": float(price) if price else None} if pr else None,
        })
    return items, d.get("page") or {}


def api_address_txs(address, page, limit=100):
    """One page of an address history (newest first) → (transactions, page info).
    to_addresses lists only the first few outputs; to_address_count has the real number."""
    d = api(f"/addresses/{address}/transactions?limit={limit}&page={page}")
    txs = [{
        "txid": t["txid"], "block_height": t.get("block_height"), "timestamp": t.get("time"),
        "sent_sats": sats(t.get("sent")), "is_coinbase": t.get("is_coinbase"),
        "to_addresses": [{"address": o.get("address"), "value_sats": sats(o.get("value"))} for o in t.get("to_addresses") or []],
        "to_address_count": t.get("to_address_count"),
    } for t in d.get("data") or []]
    return txs, d.get("page") or {}


def api_tx_outputs(txid):
    """All outputs of a transaction, summed per address."""
    out, page = {}, 1
    while True:
        d = api(f"/transactions/{txid}/outputs?limit=100&page={page}")
        items = d.get("data") or []
        for o in items:
            if o.get("address"):
                out[o["address"]] = out.get(o["address"], 0) + (sats(o.get("value")) or 0)
        if not items or not has_next(d.get("page") or {}):
            return [{"address": a, "value_sats": v} for a, v in out.items()]
        page += 1
        time.sleep(0.05)


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

CREATE TABLE IF NOT EXISTS pool_txs (
  txid TEXT NOT NULL,
  pool TEXT NOT NULL,        -- pool (coinbase) address
  height INTEGER,
  ts INTEGER,
  sent REAL,                 -- TSC sent by the pool in this tx (0 = incoming / coinbase)
  PRIMARY KEY (txid, pool)
);

CREATE TABLE IF NOT EXISTS payouts (
  txid TEXT NOT NULL,
  pool TEXT NOT NULL,
  address TEXT NOT NULL,     -- recipient (miner)
  amount REAL,               -- TSC
  height INTEGER,
  ts INTEGER,
  PRIMARY KEY (txid, pool, address)
);
CREATE INDEX IF NOT EXISTS payouts_addr ON payouts(address);
CREATE INDEX IF NOT EXISTS payouts_ts ON payouts(ts);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);
"""


PROOF_COLS = [("proof_version", "INTEGER"), ("proof_mult", "REAL"), ("p_late", "REAL"), ("p_profile", "REAL"),
              ("p_state", "REAL"), ("p_pin", "REAL"), ("near_pin", "INTEGER")]
V4_PRICING = 26950


def db():
    os.makedirs(DATA_DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    have = {r[1] for r in con.execute("PRAGMA table_info(blocks)")}
    for col, typ in PROOF_COLS:           # proof-v4 fields, added 30 Sep 2026
        if col not in have:
            con.execute(f"ALTER TABLE blocks ADD COLUMN {col} {typ}")
    con.commit()
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
        pr = b.get("proof") or {}
        comp = pr.get("components") or {}
        cnd = b.get("core_normalized_difficulty")
        rows.append((
            b["height"], b["timestamp"], b.get("miner_address"),
            float(b.get("effective_difficulty") or b.get("difficulty") or 0),
            float(b.get("base_difficulty") or b.get("difficulty") or 0),
            float(b.get("effective_multiplier") or 1.0),
            float(cnd) if cnd is not None else None,
            b.get("tx_count"), b.get("size"), b.get("hash"),
            pr.get("version"), pr.get("multiplier"), comp.get("late"), comp.get("profile"), comp.get("state"), comp.get("pin"),
            pr.get("near_pin_count"),
        ))
    # The block list has no proof components or core-normalised difficulty: keep the stored ones
    # unless the block at that height changed (reorg).
    keep = ",".join(f"{c}=CASE WHEN excluded.hash IS blocks.hash THEN COALESCE(excluded.{c}, blocks.{c}) ELSE excluded.{c} END"
                    for c in ("core_norm_difficulty", "proof_version", "proof_mult", "p_late", "p_profile", "p_state", "p_pin", "near_pin"))
    con.executemany(f"""INSERT INTO blocks
        (height,timestamp,miner,difficulty,base_difficulty,multiplier,core_norm_difficulty,tx_count,size,hash,
         proof_version,proof_mult,p_late,p_profile,p_state,p_pin,near_pin)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(height) DO UPDATE SET {keep},
          timestamp=excluded.timestamp, miner=excluded.miner, difficulty=excluded.difficulty,
          base_difficulty=excluded.base_difficulty, multiplier=excluded.multiplier,
          tx_count=excluded.tx_count, size=excluded.size, hash=excluded.hash""", rows)
    return len(rows)


def fill_proof_components(con, limit=500):
    """The block list carries only the proof-v4 price; its components come from /blocks/{hash}/proof.
    Newest blocks first, at most `limit` per run (a full backfill catches up over a few runs)."""
    rows = con.execute("SELECT height, hash FROM blocks WHERE height>=? AND p_late IS NULL AND hash IS NOT NULL "
                       "ORDER BY height DESC LIMIT ?", (V4_PRICING, limit)).fetchall()
    f = lambda x: float(x) if x is not None else None
    n = 0
    for h, bh in rows:
        try:
            d = api(f"/blocks/{bh}/proof").get("data") or {}
        except Exception as e:
            log(f"  proof {h}: {e}")
            continue
        comp = d.get("price_components") or {}
        if not comp:
            continue
        con.execute("UPDATE blocks SET proof_version=COALESCE(?, proof_version), proof_mult=?, p_late=?, p_profile=?, "
                    "p_state=?, p_pin=?, near_pin=? WHERE height=? AND hash=?",
                    ((d.get("protocol_version") or {}).get("value"), f(d.get("price_multiplier")), f(comp.get("late")),
                     f(comp.get("profile")), f(comp.get("state")), f(comp.get("pin")), d.get("near_pin_count"), h, bh))
        n += 1
        time.sleep(0.05)
    con.commit()
    return n


def backfill_proofs(con, page_size=100):
    """One-off: blocks stored before proof fields were collected get re-fetched (newest first down to v4 pricing)."""
    missing = con.execute("SELECT COUNT(*) FROM blocks WHERE height>=? AND proof_version IS NULL", (V4_PRICING,)).fetchone()[0]
    if not missing or meta_get(con, "proof_backfill_done") == "1":
        return 0
    page, n = 1, 0
    while True:
        items, _ = api_blocks(page, page_size)
        if not items:
            break
        n += upsert_blocks(con, items)
        con.commit()
        if min(b["height"] for b in items) <= V4_PRICING:
            break
        page += 1
        time.sleep(0.12)
    meta_set(con, "proof_backfill_done", "1")
    con.commit()
    return n


GITEA = "https://git.tensorcash.org/api/v1/repos/tensorcash/tensorcash/releases?limit=50"


def sync_releases(con):
    """Official releases (tag, date, name, notes excerpt) — every 3 hours."""
    now = int(time.time())
    if now - int(meta_get(con, "releases_ts", 0)) < 3 * 3600:
        return None
    raw = http_json(GITEA)
    rel = [{"tag": r.get("tag_name"), "date": (r.get("published_at") or "")[:10], "name": r.get("name"),
            "body": (r.get("body") or "")[:1500]} for r in raw if not r.get("draft")]
    meta_set(con, "releases_json", json.dumps(rel, separators=(",", ":")))
    meta_set(con, "releases_ts", now)
    con.commit()
    return len(rel)


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
        items, pag = api_blocks(page, page_size)
        if not items:
            break
        fetched += upsert_blocks(con, items)
        con.commit()
        min_h = min(b["height"] for b in items)
        if page % 10 == 0 or not has_next(pag):
            log(f"  blocks: page {page}/{pag.get('total_pages', '?')}, down to height {min_h}")
        if not backfill and min_h <= max_known:
            break
        if not has_next(pag):
            break
        if max_pages and page >= max_pages:
            break
        page += 1
        time.sleep(0.15)
    fill_gaps(con)
    return fetched


def fill_gaps(con, limit=200, page_size=100):
    """Blocks can shift between pages — re-fetch the list pages that hold missing heights
    (the API looks blocks up by hash only; the list is newest first, so page = (tip − height) // size + 1)."""
    mn, mx, cnt = known_range(con)
    if mn is None or cnt == mx - mn + 1:
        return 0
    have = set(r[0] for r in con.execute("SELECT height FROM blocks"))
    missing = [h for h in range(mn, mx + 1) if h not in have]
    log(f"  block gaps: {len(missing)} (fetching up to {limit})")
    want = set(missing[:limit])
    got = 0
    try:
        first, _ = api_blocks(1, page_size)
        tip = first[0]["height"] if first else mx
        for p in sorted({(tip - h) // page_size + 1 for h in want}):
            items, _ = api_blocks(p, page_size)
            hit = [b for b in items if b["height"] in want]
            upsert_blocks(con, hit)
            want -= {b["height"] for b in hit}
            got += len(hit)
            time.sleep(0.1)
    except Exception as e:
        log(f"  block gaps: {e}")
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

def top_addresses(limit=100):
    """Largest balances (rank order) from the address list."""
    return api(f"/addresses?limit={limit}&page=1").get("data") or []


def take_snapshot(con):
    chain = api("/stats/chain").get("data") or {}
    mining = api("/stats/mining").get("data") or {}
    reward = api("/stats/reward").get("data") or {}
    mempool = api("/mempool/summary").get("data") or {}
    act = api("/stats/activity?range=30d").get("data") or {}
    ms = http_json(f"{EXPLORER}/api/market-summary")
    now = int(time.time())

    def f(x):
        try:
            return float(str(x).replace("%", ""))
        except (TypeError, ValueError):
            return None

    tip = chain.get("latest_block") or {}
    supply_sats = sats(chain.get("indexed_supply"))
    m = {   # the explorer's former /api/home metrics, rebuilt from the stats endpoints
        "height": tip.get("height"), "tip_timestamp": tip.get("time"),
        "holders": chain.get("funded_addresses"),
        "mempool_transactions": mempool.get("transactions") if mempool.get("state") == "available" else None,
        "network_work_rate_24h": f((mining.get("recent_24h") or {}).get("rate")),
        "network_work_rate_50": f((mining.get("current_50") or {}).get("rate")),
        "difficulty": f(mining.get("tip_effective_work")),
        "current_block_reward_sats": int(reward["current_reward_units"]) if reward.get("current_reward_units") else None,
        "next_reward_height": reward.get("next_reduction_height"),
        "supply_sats": supply_sats,
    }
    # concentration: share of the indexed supply held by the top 10 / top 100 addresses
    top = [sats(h.get("balance")) or 0 for h in top_addresses(100)]
    conc = {"top_10_percent": round(sum(top[:10]) / supply_sats * 100, 2) if supply_sats and top else None,
            "top_100_percent": round(sum(top) / supply_sats * 100, 2) if supply_sats and top else None}
    sup = {"concentration": conc, "reward": reward}   # no "epochs": build.py then uses its own reward schedule

    circ = (ms.get("circulating_supply_sats") or supply_sats or 0) / SATS
    total = (ms.get("total_supply_sats") or 0) / SATS
    con.execute("""INSERT OR REPLACE INTO snapshots VALUES
        (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        now, m.get("height"), m.get("tip_timestamp"),
        f(ms.get("price_usdt")), f(ms.get("price_change_percent_24h")),
        f(ms.get("volume_24h_tsc")), f(ms.get("volume_24h_usdt")),
        circ, total, f(ms.get("circulating_market_cap_usdt")), f(ms.get("total_market_cap_usdt")),
        m.get("holders"), m.get("mempool_transactions"),
        m.get("network_work_rate_24h"), m.get("network_work_rate_50"),
        m.get("difficulty"), (m.get("current_block_reward_sats") or 0) / SATS,
        m.get("next_reward_height"),
        conc.get("top_10_percent"), conc.get("top_100_percent"),
        json.dumps({"metrics": m, "market": {k: v for k, v in ms.items() if k != "status"},
                    "supply": sup, "mining": mining}),
    ))
    # daily activity (transactions, transfers, fees...) — overwrite the last 30 days; the API no
    # longer reports new addresses per day, so values collected earlier are kept
    for d in act.get("series") or []:
        con.execute("""INSERT INTO daily_activity VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(day) DO UPDATE SET blocks=excluded.blocks, transactions=excluded.transactions,
              transfers=excluded.transfers, new_addresses=COALESCE(excluded.new_addresses, daily_activity.new_addresses),
              fees_sats=excluded.fees_sats, gross_output_sats=excluded.gross_output_sats, updated_ts=excluded.updated_ts""", (
            d["time"], d.get("blocks"), d.get("transactions"), d.get("transfers"), None,
            int(d["fee_units"]) if d.get("fee_units") is not None else None,
            int(d["output_units"]) if d.get("output_units") is not None else None, now))
    # pool aliases
    try:
        al = (api("/addresses/aliases").get("data") or {}).get("aliases") or {}
        for addr, name in al.items():
            con.execute("INSERT OR REPLACE INTO aliases VALUES (?,?,?,?)", (addr, name, "tscscan", now))
    except Exception as e:
        log(f"  aliases: {e}")
    con.commit()
    return now, m.get("height")


def sync_holders(con, ts):
    """Top 100 addresses; supply share against the circulating supply of the snapshot taken at `ts`."""
    row = con.execute("SELECT circ_supply FROM snapshots WHERE ts=?", (ts,)).fetchone()
    circ = row[0] if row else None
    n = 0
    for rank, h in enumerate(top_addresses(100), 1):
        bal = (sats(h.get("balance")) or 0) / SATS
        f7, f30 = sats(h.get("net_flow_7d")), sats(h.get("net_flow_30d"))
        con.execute("""INSERT OR REPLACE INTO holders_snapshots VALUES (?,?,?,?,?,?,?,?,?,?)""", (
            ts, rank, h.get("address"), bal,
            f7 / SATS if f7 is not None else None,
            f30 / SATS if f30 is not None else None,
            bal / circ * 100 if circ else None, h.get("transaction_count"), None,
            (h.get("last_activity") or {}).get("time")))
        n += 1
    con.commit()
    return n


# ---------------------------------------------------------------- pool payouts

def pool_addresses(con, min_share=0.01):
    """Addresses labelled as pools by the explorer (Tigerpool, Luckypool, ...) plus every address
    that found ≥1% of blocks in the last 7 days (unlabelled pools are detected later by their
    batch payouts; plain transfers of solo miners are ignored at build time)."""
    out = [r[0] for r in con.execute("SELECT address FROM aliases WHERE lower(alias) LIKE '%pool%'")]
    tip = con.execute("SELECT MAX(timestamp) FROM blocks").fetchone()[0] or 0
    rows = con.execute("SELECT miner, COUNT(*) FROM blocks WHERE timestamp > ? GROUP BY miner", (tip - 7 * 86400,)).fetchall()
    total = sum(n for _, n in rows) or 1
    out += [m for m, n in sorted(rows, key=lambda r: -r[1]) if m and n / total >= min_share and m not in out]
    return out


def sync_pool_payouts(con, pool, page_size=100, max_pages=400):
    """Walk the pool address history (newest first). Every tx the pool sends is a payout batch:
    each output to another address = one miner payout. Stops at the first fully known page."""
    known = set(r[0] for r in con.execute("SELECT txid FROM pool_txs WHERE pool=?", (pool,)))
    fresh = not known or meta_get(con, "payouts_done_" + pool) != "1"   # full history not walked yet
    page, new_tx, new_pay = 1, 0, 0
    while page <= max_pages:
        txs, pag = api_address_txs(pool, page, page_size)
        if not txs:
            break
        unseen = [t for t in txs if t.get("txid") not in known]
        for t in unseen:
            sent = (t.get("sent_sats") or 0) / SATS
            outs = t.get("to_addresses") or []
            if sent > 0 and not t.get("is_coinbase") and (t.get("to_address_count") or 0) > len(outs):
                outs = api_tx_outputs(t["txid"])   # the history lists only the first outputs of a batch
            con.execute("INSERT OR IGNORE INTO pool_txs VALUES (?,?,?,?,?)",
                        (t["txid"], pool, t.get("block_height"), t.get("timestamp"), sent))
            new_tx += 1
            if sent <= 0 or t.get("is_coinbase"):
                continue
            for o in outs:
                a = o.get("address")
                if not a or a == pool:
                    continue
                con.execute("INSERT OR IGNORE INTO payouts VALUES (?,?,?,?,?,?)",
                            (t["txid"], pool, a, (o.get("value_sats") or 0) / SATS, t.get("block_height"), t.get("timestamp")))
                new_pay += 1
        con.commit()
        if not unseen and not fresh:
            break
        if not has_next(pag):
            meta_set(con, "payouts_done_" + pool, "1")
            con.commit()
            break
        page += 1
        time.sleep(0.12)
    return new_tx, new_pay


def sync_all_payouts(con):
    for pool in pool_addresses(con):
        try:
            t, p = sync_pool_payouts(con, pool)
            log(f"  payouts {pool[:12]}…: +{t} txs, +{p} payouts")
        except Exception as e:
            log(f"  payouts {pool[:12]}…: {e}")


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
        if k != "last_collect_ts" and not k.startswith("payouts_done_") and k not in ("proof_backfill_done", "releases_ts"):
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

    try:
        k = backfill_proofs(con)
        if k:
            log(f"proof fields backfilled: {k} blocks")
        k = fill_proof_components(con)
        if k:
            log(f"proof components: {k} blocks")
    except Exception as e:
        log(f"ERROR proof backfill: {e}")

    try:
        k = sync_releases(con)
        if k is not None:
            log(f"releases: {k}")
    except Exception as e:
        log(f"ERROR releases: {e}")

    try:
        sync_all_payouts(con)
    except Exception as e:
        log(f"ERROR payouts: {e}")

    meta_set(con, "last_collect_ts", int(time.time()))
    con.commit()
    export_state(con)
    con.close()
    log(f"done ({time.time() - t0:.1f}s)")


if __name__ == "__main__":
    main(sys.argv[1:])
