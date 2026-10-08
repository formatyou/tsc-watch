"""
tsc-watch — miner tools: pool payouts → per-miner stats, pool comparison, miner lookup page,
profitability calculator and reward-epoch countdown.

Pool miners never appear in block data (the pool's address finds the block). Their earnings are
visible in the pool's payout transactions, which collect.py stores in the `payouts` table.
"""
import json
import statistics
from collections import defaultdict

import costs

DAY = 86400
REF_GPU_POI = 18.6            # RTX 5090, see costs.GPUS
MIN_BATCH = 5                 # a send with ≥ this many recipients counts as a pool payout batch
EPOCH_CUT = 0.6               # reward multiplier at each new epoch (−40%)

# Published by the pools themselves and on tensorcash.org/pools (checked 8 Oct 2026).
# "alias" = explorer label of the pool's coinbase address; "addr" = payout address listed by tensorcash.org/pools
# (used when the explorer has no label for it).
POOL_DIR = [
    {"name": "Tiger Pool", "alias": "Tigerpool", "addr": "tc1qh3rce3jqcf7an5uljx9gnxreckdl7juf56l2rh",
     "url": "https://tsc-miner.tiger-pool.com/", "fee": 3.0, "scheme": "PPLNS", "min": "1 TSC", "payout": "daily, 07:00–09:00 UTC"},
    {"name": "Bob Labs", "alias": "Bob Labs", "addr": "tc1qul4c24dyrdzgjre9jkruywv9cadsel0xdn8r39",
     "url": "https://pool.tensorcash.boblabs.eu/", "fee": 3.0, "scheme": "PPLNS", "min": "0.1 TSC", "payout": "per matured block (~17 h)"},
    {"name": "AriaPool", "alias": "AriaPool", "addr": "tc1q80y237ksyqtdaln5zjr599l0fartmvzsykvtwv",
     "url": "https://pool.ariabrain.com/tsc.html", "fee": 1.0, "scheme": "PPLNS (60 min window)", "min": "1 TSC", "payout": "per matured block (~17 h)"},
    {"name": "LuckyPool", "alias": "Luckypool", "url": "https://tensorcash.luckypool.io/", "fee": 3.0,
     "scheme": "PPLNS + solo", "min": "0.1 TSC", "payout": "—"},
    {"name": "suprnova", "alias": None, "url": "https://tsc.suprnova.cc/", "fee": 0.0,
     "scheme": "PPLNS", "min": "—", "payout": "—"},
]
POOL_DIR_DATE = "8 Oct 2026"
DIR_ADDR = {p["addr"]: p for p in POOL_DIR if p.get("addr")}


def _fee_for(alias):
    for p in POOL_DIR:
        if alias and alias.lower() in {(p["alias"] or "").lower(), p["name"].lower()}:
            return p["fee"]
    return None


# ---------------------------------------------------------------- data

def load_payouts(con):
    try:
        pays = con.execute("SELECT pool,address,amount,ts,txid FROM payouts ORDER BY ts").fetchall()
        sends = con.execute("SELECT pool,txid,ts,sent FROM pool_txs WHERE sent>0 ORDER BY ts").fetchall()
    except Exception:
        return [], []
    return pays, sends


def compute(con, d, c, reward_at, price):
    blocks, epochs = d["blocks"], c["epochs"]
    ref, tip_h = c["ref"], c["tip_h"]
    w7, w24, w30 = c["7d"], c["24h"], c["30d"]
    net_new_day = w7["new_tsc"] / 7 if w7["blocks"] else w24["new_tsc"]
    net_rate = w7["rate"] or w24["rate"]
    pays, sends = load_payouts(con)
    aliases = dict(d["aliases"])
    for a, p in DIR_ADDR.items():                          # pools whose address has no explorer label
        aliases[a] = aliases.get(a) or p["alias"] or p["name"]

    # --- which crawled addresses behave like pools (batch payouts)
    batches = defaultdict(list)          # pool -> [(txid, ts, n_recipients, sent)]
    per_tx = defaultdict(int)
    for pool, addr, amt, ts, txid in pays:
        per_tx[(pool, txid)] += 1
    for pool, txid, ts, sent in sends:
        batches[pool].append((txid, ts, per_tx.get((pool, txid), 0), sent))
    pools = []
    for pool, bl in batches.items():
        big = [b for b in bl if b[2] >= MIN_BATCH]
        name = aliases.get(pool)
        if len(big) >= 2 or (name and "pool" in name.lower() and big):
            pools.append(pool)
    for a, name in aliases.items():                        # labelled pools with no payouts yet
        if name and ("pool" in name.lower() or a in DIR_ADDR) and a not in pools:
            pools.append(a)
    pool_set = set(pools)
    big_tx = {(p, b[0]) for p in pools for b in batches.get(p, []) if b[2] >= MIN_BATCH}

    day0 = ref // DAY * DAY - 29 * DAY                     # 30 UTC days incl. today
    miners = {}

    def rec(addr):
        return miners.setdefault(addr, {"kind": 0, "pool": None, "total": 0.0, "n": 0, "first": None, "last": None,
                                        "t7": 0.0, "t30": 0.0, "daily": defaultdict(float)})

    # block finders (solo miners and the pools' own coinbase addresses)
    for h, ts, m, *_ in blocks:
        if not m:
            continue
        r = rec(m)
        rw = reward_at(epochs, h)
        r["total"] += rw
        r["n"] += 1
        r["first"] = r["first"] or ts
        r["last"] = ts
        if ts > ref - 7 * DAY:
            r["t7"] += rw
        if ts > ref - 30 * DAY:
            r["t30"] += rw
        if ts >= day0:
            r["daily"][(ts - day0) // DAY] += rw

    # pool payouts → pool miners
    for pool, addr, amt, ts, txid in pays:
        if pool not in pool_set or (pool, txid) not in big_tx or addr in pool_set:
            continue
        r = rec(addr)
        if addr in c["all"]["miners"]:
            continue                                        # finds blocks itself: keep the solo view
        r["kind"], r["pool"] = 1, pool
        r["total"] += amt
        r["n"] += 1
        r["first"] = r["first"] or ts
        r["last"] = ts
        if ts > ref - 7 * DAY:
            r["t7"] += amt
        if ts > ref - 30 * DAY:
            r["t30"] += amt
        if ts >= day0:
            r["daily"][(ts - day0) // DAY] += amt

    def est_rate(r):
        tsc_day = r["t7"] / 7
        if not tsc_day or not net_new_day:
            return 0.0
        if r["kind"] == 1:                                  # gross up by the pool fee (payouts are after fee)
            fee = _fee_for(aliases.get(r["pool"])) or 0.0
            tsc_day /= (1 - fee / 100)
        return tsc_day / net_new_day * net_rate

    for a, r in miners.items():
        r["rate"] = est_rate(r)

    # --- pool stats
    pool_rows = []
    for p in pools:
        bl = [b for b in batches.get(p, []) if b[2] >= MIN_BATCH]
        m7 = w7["miners"].get(p, {}).get("blocks", 0)
        m24 = w24["miners"].get(p, {}).get("blocks", 0)
        m30 = w30["miners"].get(p, {}).get("blocks", 0)
        rec30 = [x for x in pays if x[0] == p and x[3] > ref - 7 * DAY and (p, x[4]) in big_tx and x[1] not in pool_set]
        active7 = len({x[1] for x in rec30})
        paid30 = sum(x[2] for x in pays if x[0] == p and x[3] > ref - 30 * DAY and (p, x[4]) in big_tx and x[1] not in pool_set)
        mined30 = sum(reward_at(epochs, b[0]) for b in w30["_list"] if b[2] == p)
        bts = sorted(b[1] for b in bl)
        gaps = [bts[i + 1] - bts[i] for i in range(len(bts) - 1) if bts[i + 1] > ref - 30 * DAY]
        name = aliases.get(p)
        pool_rows.append({"addr": p, "name": name or ("Unlabelled pool " + p[:10] + "…"), "labelled": bool(name),
                          "fee": _fee_for(name), "s24": m24 / w24["blocks"] * 100 if w24["blocks"] else 0,
                          "s7": m7 / w7["blocks"] * 100 if w7["blocks"] else 0, "s30": m30 / w30["blocks"] * 100 if w30["blocks"] else 0,
                          "b7": m7, "active7": active7, "batches7": len([b for b in bl if b[1] > ref - 7 * DAY]),
                          "gap": statistics.median(gaps) if gaps else None, "last": bts[-1] if bts else None,
                          "paid30": paid30, "mined30": mined30})
    pool_rows.sort(key=lambda r: -r["s7"])

    # solo / private block finders (not pools) with blocks in 7 days
    solo_rows = []
    for a, m in w7["miners"].items():
        if a in pool_set or not a:
            continue
        solo_rows.append({"addr": a, "name": aliases.get(a) or a[:10] + "…" + a[-6:], "s7": m["blocks"] / w7["blocks"] * 100, "b7": m["blocks"]})
    solo_rows.sort(key=lambda r: -r["s7"])

    active = {a: r for a, r in miners.items() if r["t7"] > 0 and a not in pool_set}
    rates = sorted((r["t7"] / 7 for r in active.values()), reverse=True)
    board = sorted(active.items(), key=lambda kv: -kv[1]["rate"])[:30]
    return {"miners": miners, "pools": pools, "pool_set": pool_set, "pool_rows": pool_rows, "solo_rows": solo_rows,
            "active": len(active), "active_pool_miners": sum(1 for r in active.values() if r["kind"] == 1),
            "median_day": statistics.median(rates) if rates else None, "board": board, "day0": day0,
            "net_new_day": net_new_day, "net_rate": net_rate, "price": price, "has_payouts": bool(pays),
            "pool_names": {r["addr"]: r["name"] for r in pool_rows}, "proof": d.get("proof") or []}


def epoch_info(c, reward_at):
    epochs, tip_h = c["epochs"], c["tip_h"]
    cur = next((e for e in epochs if e[0] <= tip_h < e[1]), epochs[-1] if epochs else None)
    if not cur:
        return None
    w7 = c["7d"]
    bt = w7["block_time"] or c["24h"]["block_time"] or 600
    left = cur[1] - tip_h
    return {"start": cur[0], "end": cur[1], "reward": cur[2], "next": cur[2] * EPOCH_CUT, "left": left,
            "bt": bt, "eta": c["tip_ts"] + left * bt, "blocks_day": DAY / bt,
            "progress": (tip_h - cur[0]) / max(1, cur[1] - cur[0]) * 100, "index": epochs.index(cur) + 1}


# ---------------------------------------------------------------- export

def export_json(mc, c, agg=None):
    pools = mc["pools"]
    pidx = {p: i for i, p in enumerate(pools)}
    pm = {m[0]: m for m in (agg or {}).get("miners", [])}          # proof-v4 stats per block finder

    def mult(addr):
        x = pm.get(addr)
        if not x:
            return None
        drv = max((("late credit", x[6]), ("sampling profile", x[7]), ("state", x[8])), key=lambda t: t[1])
        return [x[2], drv[0] if drv[1] > 1.02 else None, x[1]]

    m = {}
    for a, r in mc["miners"].items():
        if a in mc["pool_set"]:
            continue
        daily = [[k, round(v, 3)] for k, v in sorted(r["daily"].items()) if v]
        m[a] = [r["kind"], pidx.get(r["pool"], -1), round(r["total"], 3), r["n"], r["first"], r["last"],
                round(r["t7"], 3), round(r["rate"], 1), daily, mult(a) if r["kind"] == 0 else None]
    wk = lambda dd: [round(sum(v for k, v in dd.items() if 15 <= k < 22), 3), round(sum(v for k, v in dd.items() if 22 <= k < 29), 3)]
    pool_daily = defaultdict(lambda: defaultdict(float))
    for a, r in mc["miners"].items():
        if r["kind"] == 1 and a not in mc["pool_set"]:
            for k, v in r["daily"].items():
                pool_daily[r["pool"]][k] += v
    net_daily = {(day - mc["day0"]) // DAY: x["new_tsc"] for day, x in c["daily"].items()}
    pool_list = []
    for p in pools:
        row = next((x for x in mc["pool_rows"] if x["addr"] == p), {})
        pool_list.append({"addr": p, "name": row.get("name", p), "fee": row.get("fee"), "gap": row.get("gap"), "mult": mult(p), "weeks": wk(pool_daily[p])})
    # network yield per UTC day: TSC one PoI/s earned that day (for hardware-vs-network trend)
    yld = []
    for day, x in c["daily"].items():
        k = (day - mc["day0"]) // DAY
        if 0 <= k < 30 and x["work"]:
            yld.append([k, round(x["new_tsc"] / (x["work"] / DAY), 6)])
    return {"generated_at": c["now"], "price": mc["price"], "net_new_tsc_day": mc["net_new_day"], "net_rate": mc["net_rate"],
            "block_time": c["7d"]["block_time"] or 600, "net_mult": (agg or {}).get("all", {}).get("mean"),
            "ref_gpu": {"name": "RTX 5090", "poi": REF_GPU_POI}, "day0": mc["day0"], "pools": pool_list, "yield": yld, "net_weeks": wk(net_daily),
            "fields": ["kind(0=block finder,1=pool miner)", "pool_index", "total_tsc", "blocks_or_payouts", "first_ts", "last_ts",
                       "tsc_7d", "est_poi_s", "daily_30d[[day_index,tsc]]", "proof_v4[mean_multiplier,main_driver,blocks] (block finders)"],
            "miners": m}


# ---------------------------------------------------------------- pages

def epoch_card(B, ep, price):
    if not ep:
        return ""
    return f"""<section class="card" id="epoch"><div class="epoch"><div><h2>Next reward cut</h2>
<p class="sub">Epoch {ep['index']} pays {B.fnum(ep['reward'], 4)} TSC per block until block #{B.fnum(ep['end'])}. Then the reward drops 40% to {B.fnum(ep['next'], 4)} TSC.</p></div>
<div class="ekpis"><div><div class="k">Blocks left</div><div class="v">{B.fnum(ep['left'])}</div></div>
<div><div class="k">Estimated date</div><div class="v" data-eta="{int(ep['eta'])}">{B.fdt(ep['eta'], '%d %b %Y')}</div><div class="s" data-left="{int(ep['eta'])}"></div></div>
<div><div class="k">Issuance after the cut</div><div class="v">{B.fnum(ep['next'] * ep['blocks_day'], 0)} TSC/day</div><div class="s">now {B.fnum(ep['reward'] * ep['blocks_day'], 0)} · ≈ {B.fusd(ep['next'] * ep['blocks_day'] * price, 0) if price else '—'}/day at today's price</div></div></div></div>
<div class="bar"><i style="width:{min(100, ep['progress']):.1f}%"></i></div>
<p class="note">Progress through epoch {ep['index']}: {B.fnum(ep['progress'], 1)}% · date assumes the 7-day average block time ({B.fdur(ep['bt'])}). If the network keeps the same work rate, each miner's TSC per day falls by 40% at the cut.</p></section>"""


BENCH_GPUS = [("B200", 56.0), ("H100 / H200", 40.0), ("RTX PRO 6000", 26.0), ("RTX 5090", 18.6), ("RTX A6000", 9.5)]
BENCH_WINDOWS = [("24h", 1), ("7d", 7), ("30d", 30)]
BENCH_MIN_BLOCKS = 3


def bench_card(B, mc, c):
    """Pool benchmark: net TSC/day of one GPU on each pool = network-average yield × (network mean
    multiplier ÷ pool's mean multiplier) × (1 − fee). All inputs are on-chain except the published fee."""
    esc, fnum, fusd = B.esc, B.fnum, B.fusd
    price, ref = mc["price"], c["ref"]
    proof = [r for r in mc.get("proof", []) if r[3] == 4 and r[4]]
    on_chain = {(r["name"] or "").lower(): r for r in mc["pool_rows"]}
    listed = set()
    entries = []                                   # (label, sub, addr, fee, pool_row)
    for r in mc["pool_rows"]:
        entries.append((r["name"], r["addr"][:14] + "…", r["addr"], r["fee"], r))
        listed.add((r["name"] or "").lower())
    for p in POOL_DIR:
        if (p.get("addr") and any(r["addr"] == p["addr"] for r in mc["pool_rows"])) or (p["alias"] and p["alias"].lower() in listed):
            continue
        entries.append((p["name"], None, None, p["fee"], None))
    bodies = []
    for key, days in BENCH_WINDOWS:
        w = c[key]
        if not w["blocks"] or not w["rate"]:
            continue
        per_poi = w["new_tsc"] / days / w["rate"]              # TSC/day per 1 PoI/s at the network average
        rows_w = [r for r in proof if r[1] > ref - days * DAY]
        net_m = sum(r[4] for r in rows_w) / len(rows_w) if rows_w else 1.0
        top = per_poi * net_m                                  # bar scale: solo at 1.0×

        def line(label, sub, y, mult, fee, ratio, bpd, conf, cls=""):
            d = (y / per_poi - 1) * 100
            col = "#2F7A55" if d > 0.5 else ("#B42318" if d < -0.5 else "inherit")
            return (f"<tr class='{cls}'><td>{label}<div class='small'>{sub}</div></td>"
                    f"<td><b class='bmv' data-y='{y:.8f}'>{fnum(y * REF_GPU_POI, 3)}</b><div class='bmbar'><i style='width:{min(100, y / top * 96):.1f}%'></i><u style='left:{per_poi / top * 96:.1f}%'></u></div></td>"
                    f"<td class='bmu' data-y='{y:.8f}'>{fusd(y * REF_GPU_POI * price, 2) if price else '—'}</td>"
                    f"<td style='color:{col};font-weight:600'>{'+' if d >= 0 else '−'}{fnum(abs(d), 1)}%</td><td>{mult}</td><td>{fee}</td><td>{ratio}</td><td>{bpd}</td><td>{conf}</td></tr>")

        meas, na = [], []
        for label, sub, addr, fee, pr in entries:
            fee_s = "—" if fee is None else f"{fnum(fee, 0)}%"
            m = [r[4] for r in rows_w if addr and r[2] == addr]
            nb = w["miners"].get(addr, {}).get("blocks", 0) if addr else 0
            sub_s = f"<span class='mono'>{esc(sub)}</span>" if sub else "no block-finding address seen"
            if len(m) < BENCH_MIN_BLOCKS:
                why = "no blocks on-chain" if not addr else (f"{fnum(nb)} blocks in this window" if nb else "no blocks in this window")
                na.append(f"<tr class='muted'><td>{esc(label)}<div class='small'>{sub_s}</div></td><td colspan='3'><span class='bmna'>not measurable yet</span></td>"
                          f"<td>—</td><td>{fee_s}</td><td>—</td><td>{fnum(nb / days, 1)}</td><td>{why}</td></tr>")
                continue
            pm = sum(m) / len(m)
            y = per_poi * net_m / pm * (1 - (fee or 0) / 100)
            ratio = f"{fnum(pr['paid30'] / pr['mined30'] * 100, 0)}%" if pr and pr["mined30"] and pr["paid30"] else "—"
            conf = ("high" if len(m) >= 100 else "medium" if len(m) >= 20 else "low") + f" · {fnum(len(m))} blocks"
            meas.append((y, line(esc(label), sub_s + ("" if fee is not None else " · fee not published, shown before fee"), y, f"{fnum(pm, 3)}×", fee_s, ratio, fnum(nb / days, 1), conf)))
        meas.sort(key=lambda x: -x[0])
        refs = [line("Network average", "reference: every block finder, blended", per_poi, f"{fnum(net_m, 3)}×", "—", "—", fnum(w["blocks"] / days, 1), "—", "bmref"),
                line("Solo at a perfect 1.0×", "ceiling: no fee, no multiplier tax", top, "1.000×", "0%", "—", "your share", "—", "bmref")]
        bodies.append((key, f"{''.join(x[1] for x in meas)}{''.join(refs)}{''.join(na)}"))
    if not bodies:
        return ""
    dflt = "7d" if any(k == "7d" for k, _ in bodies) else bodies[0][0]
    keys = [k for k, _ in bodies]
    bodies = [f"<tbody data-bw='{k}'{'' if k == dflt else ' hidden'}>{h}</tbody>" for k, h in bodies]
    gp = "".join(f"<button type='button' data-poi='{poi}'{' class=on' if poi == REF_GPU_POI else ''}>{esc(n)}</button>" for n, poi in BENCH_GPUS)
    wn = "".join(f"<button type='button' data-w='{k}'{' class=on' if k == dflt else ''}>{k}</button>" for k in keys)
    return f"""<section class="card" id="bench" data-price="{price or 0}"><h2>Pool benchmark</h2><p class="sub">What the same GPU nets per day on each pool, after the pool's fee and its intelligence multiplier. Measured from blocks on-chain, not from what pools advertise.</p>
<div class="bmctl"><div class="seg" id="bmgpu">{gp}</div><label class="bmcus">Custom PoI/s <input id="bmpoi" type="number" min="0" step="0.1" inputmode="decimal" aria-label="Custom PoI/s"></label><div class="seg" id="bmwin">{wn}</div></div>
<div style="overflow:auto"><table class="bm"><thead><tr><th>Where you mine</th><th>Net TSC / day</th><th>USD / day</th><th>vs network avg</th><th>Multiplier</th><th>Fee</th><th>Paid ÷ mined, 30d</th><th>Blocks / day</th><th>Confidence</th></tr></thead>{''.join(bodies)}</table></div>
<p class="note">Net TSC/day = network-average yield for this GPU × (network mean multiplier ÷ pool's multiplier) × (1 − fee). The thin line on each bar marks the network average. Small pools pay in bursts: with under 5 blocks a day the daily result swings a lot even when the long-run yield is the same. A pool with fewer than {BENCH_MIN_BLOCKS} blocks in the window cannot be benchmarked from the chain, whatever its fee. Multipliers: see <a href="proof.html">Proof efficiency</a>. Not an endorsement.</p></section>"""


def build_miner_page(B, mc, ep, c):
    price = mc["price"]
    esc, fnum, fusd, fdt = B.esc, B.fnum, B.fusd, B.fdt
    top_pool = mc["pool_rows"][0] if mc["pool_rows"] else None

    def ago(ts):
        if not ts:
            return "—"
        s = c["now"] - ts
        return f"{s/3600:.0f} h ago" if s < 2 * DAY else f"{s/DAY:.0f} d ago"

    kpis = f"""<div class="kpis">
<div class="kpi"><div class="k">Active miners, 7 days</div><div class="v">{fnum(mc['active'])}</div><div class="s">{fnum(mc['active_pool_miners'])} paid by pools + {fnum(mc['active'] - mc['active_pool_miners'])} solo block finders</div></div>
<div class="kpi"><div class="k">Pools seen on-chain</div><div class="v">{fnum(len(mc['pool_rows']))}</div><div class="s">addresses paying batches of ≥{MIN_BATCH} miners</div></div>
<div class="kpi"><div class="k">Largest pool, 7 days</div><div class="v">{fnum(top_pool['s7'], 1) + '%' if top_pool else '—'}</div><div class="s">{esc(top_pool['name']) if top_pool else ''} · share of blocks</div></div>
<div class="kpi"><div class="k">Median miner</div><div class="v">{fnum(mc['median_day'], 2) if mc['median_day'] else '—'} TSC/day</div><div class="s">≈ {fusd(mc['median_day'] * price, 2) if mc['median_day'] and price else '—'}/day · 7-day average of active miners</div></div>
</div>"""

    lookup = f"""<section class="hero"><h1>Miner <em>dashboard</em>.</h1>
<p class="lead">Paste a TSC address: a pool payout address or a solo miner's coinbase address. You get earnings per day, estimated proof rate and the last 30 days. Pool miners are tracked through the pools' on-chain payouts.</p>
<form id="mlook" class="look" autocomplete="off"><input id="maddr" placeholder="tc1q…" spellcheck="false" aria-label="TSC address"><button type="submit">Look up</button></form>
<div id="mres" class="mres"></div></section>"""

    rows = []
    for i, (a, r) in enumerate(mc["board"], 1):
        via = esc(mc["pool_names"].get(r["pool"], "pool")) if r["kind"] == 1 else "solo"
        rows.append(f"<tr><td>{i}</td><td><a class='mono' href='#{esc(a)}'>{esc(a[:10])}…{esc(a[-6:])}</a></td><td>{via}</td>"
                    f"<td>{fnum(r['t7'] / 7, 2)}</td><td>{fusd(r['t7'] / 7 * price, 2) if price else '—'}</td>"
                    f"<td>{B.frate(r['rate'])}</td><td>{fnum(r['rate'] / REF_GPU_POI, 1)}</td><td>{ago(r['last'])}</td></tr>")
    board = f"""<section class="card"><h2>Top miners, last 7 days</h2><p class="sub">Individual miners, including those mining through pools, ranked by estimated proof rate. Pool payouts are grossed up by the published pool fee.</p>
<div style="overflow:auto"><table><thead><tr><th>#</th><th>Address</th><th>Via</th><th>TSC / day</th><th>USD / day</th><th>Est. proof rate</th><th>≈ RTX 5090s</th><th>Last paid / block</th></tr></thead><tbody>{''.join(rows) or '<tr><td colspan=8>No data yet</td></tr>'}</tbody></table></div>
<p class="note">Estimated proof rate = miner's TSC per day ÷ network TSC per day × network work rate (7-day averages). It is an estimate: PPLNS windows, payout timing and luck move it around.</p></section>"""

    prow = []
    for p in mc["pool_rows"]:
        fee = "—" if p["fee"] is None else f"{fnum(p['fee'], 0)}%"
        ratio = f"{fnum(p['paid30'] / p['mined30'] * 100, 0)}%" if p["mined30"] and p["paid30"] else "—"
        gap = B.fdur(p["gap"]) if p["gap"] else "—"
        prow.append(f"<tr><td>{esc(p['name'])}<div class='mono small'>{esc(p['addr'][:14])}…</div></td><td>{fnum(p['s24'], 1)}%</td><td><b>{fnum(p['s7'], 1)}%</b></td><td>{fnum(p['s30'], 1)}%</td>"
                    f"<td>{fnum(p['active7'])}</td><td>{fnum(p['batches7'])}</td><td>{gap}</td><td>{ago(p['last'])}</td><td>{ratio}</td><td>{fee}</td></tr>")
    for s in mc["solo_rows"][:6]:
        prow.append(f"<tr class='muted'><td>{esc(s['name'])}<div class='small'>no batch payouts: solo or private</div></td><td>—</td><td>{fnum(s['s7'], 1)}%</td><td>—</td><td>—</td><td>—</td><td>—</td><td>—</td><td>—</td><td>—</td></tr>")
    dirrows = "".join(
        f"<tr><td><a href='{esc(p['url'])}' target='_blank' rel='noopener noreferrer nofollow'>{esc(p['name'])} ↗</a></td><td>{'—' if p['fee'] is None else fnum(p['fee'], 0) + '%'}</td>"
        f"<td>{esc(p['scheme'])}</td><td>{esc(p['min'])}</td><td>{esc(p['payout'])}</td></tr>" for p in POOL_DIR)
    pools = f"""<section class="card" id="pools"><h2>Pool comparison</h2><p class="sub">What each pool does on-chain: share of blocks, how many miners it actually paid, and how regularly. Sorted by 7-day share.</p>
<div style="overflow:auto"><table><thead><tr><th>Pool</th><th>Share 24h</th><th>Share 7d</th><th>Share 30d</th><th>Miners paid, 7d</th><th>Payout batches, 7d</th><th>Typical gap</th><th>Last payout</th><th>Paid ÷ mined, 30d</th><th>Fee</th></tr></thead><tbody>{''.join(prow) or '<tr><td colspan=10>No data yet</td></tr>'}</tbody></table></div>
<p class="note">Paid ÷ mined compares TSC paid out to miners with TSC the pool's address earned from blocks over 30 days. Around 100% minus the fee is normal. Big gaps can come from immature rewards or payouts sent from a different wallet. The fee is only half the cost: see <a href="proof.html">Proof efficiency</a> for each producer's intelligence multiplier.</p>
<h3>Pool directory</h3><div style="overflow:auto"><table><thead><tr><th>Pool</th><th>Fee</th><th>Scheme</th><th>Minimum payout</th><th>Payouts</th></tr></thead><tbody>{dirrows}</tbody></table></div>
<p class="note">As published on the pools' websites, {POOL_DIR_DATE}. Not an endorsement. Spreading work across pools keeps the network decentralised.</p></section>"""
    return lookup + kpis + epoch_card(B, ep, price) + bench_card(B, mc, c) + pools + board


def build_calc_page(B, c, ep, price):
    gpus = [{"name": n, "poi": p, "w": w, "rent": r} for n, p, w, r in costs.GPUS]
    params = {"price": price, "net_rate": c["24h"]["rate"], "new_day": c["24h"]["new_tsc"],
              "reward": ep["reward"] if ep else None, "left": ep["left"] if ep else None,
              "blocks_day": ep["blocks_day"] if ep else 144, "gpus": gpus, "elec": costs.ELEC_USD_KWH}
    opts = "".join(f'<option value="{i}"{" selected" if g["name"] == costs.REF_GPU else ""}>{B.esc(g["name"])} · {B.fnum(g["poi"], 1)} PoI/s</option>' for i, g in enumerate(gpus))
    return f"""<section class="hero"><h1>Mining <em>profitability</em> calculator.</h1>
<p class="lead">Estimate what your GPUs earn on TensorCash today and after the next reward cut. Network data is taken from the chain (last 24 hours) and refreshed with the site. Change any number. New to TensorCash? <a href="start.html">Check your GPU and get a ready config</a>.</p></section>
<div class="calc"><section class="card"><h2>Your setup</h2>
<label>GPU<select id="c_gpu">{opts}<option value="custom">Custom</option></select></label>
<div class="row2"><label>PoI/s per GPU<input id="c_poi" type="number" step="0.1" min="0"></label><label>Power per GPU, W<input id="c_w" type="number" step="10" min="0"></label></div>
<div class="row2"><label>Number of GPUs<input id="c_n" type="number" min="1" step="1" value="1"></label><label>Pool fee, %<input id="c_fee" type="number" min="0" max="100" step="0.5" value="1"></label></div>
<div class="seg" id="c_mode"><button type="button" data-m="own" class="on">Own hardware</button><button type="button" data-m="rent">Rented GPU</button></div>
<div class="row2 m-own"><label>Electricity, $/kWh<input id="c_elec" type="number" min="0" step="0.01"></label><label>Hardware cost per GPU, $<input id="c_hw" type="number" min="0" step="50" value="0"></label></div>
<div class="row2 m-rent" hidden><label>Rent per GPU, $/hour<input id="c_rent" type="number" min="0" step="0.01"></label><label>&nbsp;<span class="note">Electricity is included in rental prices.</span></label></div>
<h3>Market &amp; network</h3>
<div class="row2"><label>TSC price, $<input id="c_price" type="number" min="0" step="0.01"></label><label>Network work rate, PoI/s<input id="c_net" type="number" min="1" step="100"></label></div>
<label>Network growth, % per month<input id="c_g" type="number" step="5" value="0"></label>
<p class="note" id="c_src"></p></section>
<section class="card"><h2>Results</h2><div class="kpis res" id="c_out"></div>
<h3>Today vs. after the next reward cut</h3><div style="overflow:auto"><table id="c_tab"></table></div>
<h3>Cumulative profit</h3><div id="c_proj" style="overflow:auto"></div>
<p class="note">Assumptions: blocks keep coming at the recent pace; the reward drops 40% at each epoch end; your share = your PoI/s ÷ network PoI/s. Price is held constant. Real results vary with luck, pool and uptime. Not financial advice.</p></section></div>
<script>window.TSC_CALC={json.dumps(params)}</script>"""


CALC_JS = r"""
(function(){const P=window.TSC_CALC;if(!P)return;const $=id=>document.getElementById(id);
const f2=(x,d=2)=>x==null||!isFinite(x)?'—':x.toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d});
const usd=(x,d=2)=>x==null||!isFinite(x)?'—':(x<0?'−$':'$')+f2(Math.abs(x),d);
let mode='own';const ids=['c_gpu','c_poi','c_w','c_n','c_fee','c_elec','c_hw','c_rent','c_price','c_net','c_g'];
function setGpu(){const v=$('c_gpu').value;if(v==='custom')return;const g=P.gpus[+v];$('c_poi').value=g.poi;$('c_w').value=g.w;$('c_rent').value=g.rent==null?'':g.rent}
function load(){$('c_price').value=(+P.price).toFixed(3);$('c_net').value=Math.round(P.net_rate);$('c_elec').value=P.elec;setGpu();
 try{const h=new URLSearchParams(location.hash.slice(1));ids.forEach(i=>{if(h.has(i))$(i).value=h.get(i)});if(h.get('mode'))setMode(h.get('mode'),true)}catch(e){}
 $('c_src').textContent='Chain data, last 24h: '+f2(P.new_day,0)+' new TSC/day, '+f2(P.net_rate,0)+' PoI/s network. Reward now '+f2(P.reward,4)+' TSC/block, '+f2(P.left,0)+' blocks to the next cut.'}
function setMode(m,quiet){mode=m;document.querySelectorAll('#c_mode button').forEach(b=>b.classList.toggle('on',b.dataset.m===m));
 document.querySelectorAll('.m-own').forEach(e=>e.hidden=m!=='own');document.querySelectorAll('.m-rent').forEach(e=>e.hidden=m!=='rent');if(!quiet)calc()}
function sim(days,x){let prof=0,rate=x.net,left=P.left==null?Infinity:P.left,rew=P.reward||0,out=[];const g=Math.pow(1+x.g/100,1/30);
 for(let d=1;d<=days;d++){let blocks=P.blocks_day,iss=0;while(blocks>0){const b=Math.min(blocks,left>0?left:blocks);iss+=b*rew;blocks-=b;left-=b;if(left<=0){rew*=0.6;left=Infinity}}
  const tsc=iss*x.my/rate*(1-x.fee/100);prof+=tsc*x.price-x.cost;rate*=g;out.push(prof)}return out}
function calc(){const x={poi:+$('c_poi').value||0,w:+$('c_w').value||0,n:Math.max(1,+$('c_n').value||1),fee:+$('c_fee').value||0,elec:+$('c_elec').value||0,
 hw:+$('c_hw').value||0,rent:+$('c_rent').value||0,price:+$('c_price').value||0,net:Math.max(1,+$('c_net').value||1),g:+$('c_g').value||0};
 x.my=x.poi*x.n;const share=x.my/(x.net+ (0));const newDay=(P.reward||0)*P.blocks_day;const tsc=newDay*x.my/x.net*(1-x.fee/100);
 x.cost=mode==='own'?x.w*x.n/1000*24*x.elec:x.rent*x.n*24;const rev=tsc*x.price,prof=rev-x.cost;
 const be=tsc?x.cost/tsc:null;const roi=mode==='own'&&x.hw>0&&prof>0?x.hw*x.n/prof:null;
 const tscA=tsc*0.6,revA=tscA*x.price,profA=revA-x.cost;
 $('c_out').innerHTML=[['TSC per day',f2(tsc,3),'share of network '+f2(share*100,3)+'%'],['Revenue per day',usd(rev),usd(rev*30,0)+' per 30 days'],
  ['Cost per day',usd(x.cost),mode==='own'?f2(x.w*x.n*24/1000,1)+' kWh/day':'rent '+usd(x.rent*x.n,2)+'/h'],
  ['Profit per day','<span class="'+(prof>=0?'up':'down')+'">'+usd(prof)+'</span>',usd(prof*30,0)+' per 30 days'],
  ['Break-even TSC price',usd(be,3),'cost per mined TSC'],['Payback of hardware',roi?f2(roi,0)+' days':'—',mode==='own'?(x.hw?'at today\'s profit':'enter hardware cost'):'n/a for rented GPUs']]
  .map(r=>'<div class="kpi"><div class="k">'+r[0]+'</div><div class="v">'+r[1]+'</div><div class="s">'+r[2]+'</div></div>').join('');
 $('c_tab').innerHTML='<thead><tr><th></th><th>Today</th><th>After cut (−40%)</th></tr></thead><tbody>'+
  [['TSC per day',f2(tsc,3),f2(tscA,3)],['Revenue per day',usd(rev),usd(revA)],['Profit per day',usd(prof),usd(profA)],['Break-even price',usd(be,3),usd(tscA?x.cost/tscA:null,3)]]
  .map(r=>'<tr><td>'+r[0]+'</td><td>'+r[1]+'</td><td>'+r[2]+'</td></tr>').join('')+'</tbody>';
 const s=sim(365,x);$('c_proj').innerHTML='<table><thead><tr><th>After</th><th>30 days</th><th>90 days</th><th>180 days</th><th>365 days</th></tr></thead><tbody><tr><td>Profit'+(mode==='own'&&x.hw?' (before hardware)':'')+'</td>'+
  [30,90,180,365].map(d=>'<td class="'+(s[d-1]>=0?'':'down')+'">'+usd(s[d-1],0)+'</td>').join('')+'</tr>'+(mode==='own'&&x.hw?'<tr><td>Net of hardware</td>'+[30,90,180,365].map(d=>{const v=s[d-1]-x.hw*x.n;return '<td class="'+(v>=0?'up':'down')+'">'+usd(v,0)+'</td>'}).join('')+'</tr>':'')+'</tbody></table>';
 const h=new URLSearchParams();ids.forEach(i=>h.set(i,$(i).value));h.set('mode',mode);history.replaceState(null,'','#'+h.toString())}
load();$('c_gpu').addEventListener('change',()=>{setGpu();calc()});ids.forEach(i=>$(i).addEventListener('input',()=>{if(['c_poi','c_w'].includes(i))$('c_gpu').value='custom';calc()}));
document.querySelectorAll('#c_mode button').forEach(b=>b.addEventListener('click',()=>setMode(b.dataset.m)));calc()})();
"""

MINER_JS = r"""
(function(){const F=document.getElementById('mlook');if(!F)return;const R=document.getElementById('mres');let D=null;
const f2=(x,d=2)=>x==null||!isFinite(x)?'—':x.toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d});
const usd=(x,d=2)=>x==null||!isFinite(x)?'—':'$'+f2(x,d);const MON=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const dt=t=>{if(!t)return '—';const d=new Date(t*1000);return d.getUTCDate()+' '+MON[d.getUTCMonth()]+' '+d.getUTCFullYear()};
const rate=x=>x>=1e3?f2(x/1e3,1)+' K PoI/s':f2(x,1)+' PoI/s';const esc=s=>String(s).replace(/[&<>"']/g,c=>'&#'+c.charCodeAt(0)+';');
async function data(){if(!D){R.innerHTML='<p class="note">Loading miner data…</p>';D=await fetch('/miners.json',{cache:'no-cache'}).then(r=>r.json())}return D}
function bars(daily,day0){const W=900,H=200,pl=58,pr=16,pt=14,pb=30,n=30,bw=(W-pl-pr)/n;const v=Array(n).fill(0);daily.forEach(([k,x])=>{if(k>=0&&k<n)v[k]+=x});
 const mx=Math.max(...v,0.0001)*1.08;const Y=y=>pt+(H-pt-pb)-y/mx*(H-pt-pb);let s='<svg viewBox="0 0 '+W+' '+H+'" class="chart" role="img">';
 for(let i=0;i<=4;i++){const t=mx*i/4,y=Y(t);s+='<line x1="'+pl+'" x2="'+(W-pr)+'" y1="'+y+'" y2="'+y+'" class="grid"/><text x="'+(pl-6)+'" y="'+(y+4)+'" class="tick" text-anchor="end">'+f2(t,t<10?1:0)+'</text>'}
 const bs=[];v.forEach((x,i)=>{const X=pl+i*bw;s+='<rect x="'+(X+bw*.12)+'" y="'+Y(x)+'" width="'+(bw*.76)+'" height="'+(pt+(H-pt-pb)-Y(x))+'" fill="#731D30" opacity=".85"/>';
  if(i%5===0){const d=new Date((day0+i*86400)*1000);s+='<text x="'+(X+bw/2)+'" y="'+(H-8)+'" class="tick" text-anchor="middle">'+d.getUTCDate()+' '+MON[d.getUTCMonth()]+'</text>'}
  bs.push([+(X+bw/2).toFixed(1),day0+i*86400,[['TSC',  '#731D30',f2(x,3)+' TSC ≈ '+usd(x*D.price)]]])});
 s+='</svg>';const w=document.createElement('div');w.className='cw';w.innerHTML=s;const svg=w.firstChild;
 svg.setAttribute('data-tip',JSON.stringify({d:1,b:[pt,H-pb],l:pl,r:W-pr,bw:+bw.toFixed(1),bars:bs}));return w}
function health(d,kind,pool,last,est,daily,pm,m0first){const H=[],now=d.generated_at,hh=x=>x<1?Math.max(1,Math.round(x*60))+' min':x<10?f2(x,1)+' h':x<48?Math.round(x)+' h':f2(x/24,1)+' days',rank={ok:0,warn:1,bad:2};
 const age=(now-last)/3600;
 if(kind===1){const gap=((pool&&pool.gap)||86400)/3600;const lvl=age<=1.5*gap+3?'ok':age<=3*gap?'warn':'bad';
  H.push([lvl,'Payouts',lvl==='ok'?'Last payout '+hh(age)+' ago. '+'<span>'+(pool?esc(pool.name):'Your pool')+'</span>'+' pays about every '+hh(gap)+'.':'No payout for '+hh(age)+', while '+'<span>'+(pool?esc(pool.name):'your pool')+'</span>'+' pays about every '+hh(gap)+'. Check that the rig is online and on the current miner version.'])}
 else if(est>0){const exp=d.block_time*d.net_rate/est/3600;const lvl=age<=3*exp?'ok':age<=6*exp?'warn':'bad';
  H.push([lvl,'Blocks',(lvl==='ok'?'Last block '+hh(age)+' ago. ':'No block for '+hh(age)+'. ')+'At this rate expect one about every '+hh(exp)+'.'+(lvl!=='ok'?' Check the node, the miner version and the verifier.':'')])}
 else H.push(['bad','Blocks','No block in the last 7 days.']);
 const v=Array(30).fill(0);daily.forEach(([k,x])=>{if(k>=0&&k<30)v[k]+=x});
 const sum=(a,i,j)=>a.slice(i,j).reduce((s,x)=>s+x,0);const e1=sum(v,22,29),e0=sum(v,15,22);
 const first=m0first;if(first>d.day0+15*86400)H.push(['ok','Weekly trend','Mining here for less than two weeks; the trend appears after 14 days.']);
 else if(kind===0&&e0>0&&e0<5*50)H.push(['ok','Weekly trend','Too few blocks per week for a reliable trend; luck dominates.']);
 else if(e0>0){const ref=kind===1?(pool&&pool.weeks):d.net_weeks;const pct=x=>(x>=0?'+':'')+Math.round(x)+'%';const ch=(e1/e0-1)*100;
  if(ref&&ref[0]>0&&ref[1]>0){const rc=(ref[1]/ref[0]-1)*100,rel=((e1/e0)/(ref[1]/ref[0])-1)*100,lvl=rel>=-25?'ok':rel>=-50?'warn':'bad';
   H.push([lvl,'Weekly trend','Earned '+pct(ch)+' vs the week before; '+(kind===1?'<span>'+(pool?esc(pool.name):'the pool')+'</span>'+' paid all its miners '+pct(rc):'the network issued '+pct(rc))+'. '+(kind===1?'Your share of the pool: ':'Your share of blocks: ')+pct(rel)+'.'+(lvl!=='ok'?' Falling behind '+(kind===1?'the rest of the pool':'the network')+' usually means rigs offline, an old miner version or rejected work.':'')])}
  else H.push(['ok','Weekly trend','Earned '+pct(ch)+' vs the week before.'])}
 const pmx=kind===1?(pool&&pool.mult):pm;if(pmx){const mm=pmx[0],lim=Math.max(1.05,(d.net_mult||1)+0.02),lvl=mm<=lim?'ok':mm<=1.15?'warn':'bad',who=kind===1?'Your pool\'s blocks':'Your blocks';
  H.push([lvl,'Proof multiplier',who+' average '+f2(mm,3)+'× (network '+f2(d.net_mult,3)+'×), '+(mm>1?'about '+Math.round((1-1/mm)*100)+'% of the work goes to the multiplier':'no multiplier tax')+(pmx[1]?', mostly '+pmx[1]:'')+'. <a href="proof.html">Details</a>'])}
 const worst=H.reduce((w,h)=>rank[h[0]]>rank[w]?h[0]:w,'ok');const title={ok:'Looks healthy',warn:'Worth a look',bad:'Needs attention'}[worst];
 return '<div class="health '+worst+'"><div class="hh"><span class="hdot"></span><b>'+title+'</b><span class="note">checked '+dt(now)+'</span></div>'+H.map(h=>'<div class="hrow"><span class="pill '+(h[0]==='ok'?'up':h[0]==='warn'?'warn':'down')+'">'+(h[0]==='ok'?'ok':h[0]==='warn'?'check':'alert')+'</span><span><b>'+h[1]+'.</b> '+h[2]+'</span></div>').join('')+'</div>'}
async function show(a){a=(a||'').trim();if(!a)return;const d=await data();const m=d.miners[a];
 if(!m){R.innerHTML='<div class="mcard"><b>No mining income found for this address.</b><p class="note">We track solo block finders and payouts from pools that pay on-chain in batches. Payouts sent from a separate pool wallet, or very new addresses, may be missing. <a href="https://tscscan.xyz/address/'+encodeURIComponent(a)+'" target="_blank" rel="noopener noreferrer">Open in explorer ↗</a></p></div>';return}
 const [kind,pi,total,n,first,last,t7,est,daily,pm]=m;const pool=pi>=0?d.pools[pi]:null;const perDay=t7/7;
 const via=kind===1?'Pool miner · paid by '+'<span>'+esc(pool?pool.name:'a pool')+'</span>'+(pool&&pool.fee!=null?' (fee '+pool.fee+'%)':''):'Solo miner · finds blocks directly';
 const k=[['TSC per day, 7-day avg',f2(perDay,3),usd(perDay*d.price)+' per day at '+usd(d.price,3)],['Estimated proof rate',est?rate(est):'—',est?'≈ '+f2(est/d.ref_gpu.poi,1)+' × '+d.ref_gpu.name:'no income in the last 7 days'],
  ['Earned since first seen',f2(total,2)+' TSC','≈ '+usd(total*d.price,0)+' at today\'s price'],[kind===1?'Payouts received':'Blocks found',f2(n,0),'first '+dt(first)+' · last '+dt(last)]];
 R.innerHTML='<div class="mcard"><div class="mhead"><span class="badge">'+via+'</span> <a class="mono" href="https://tscscan.xyz/address/'+encodeURIComponent(a)+'" target="_blank" rel="noopener noreferrer">'+esc(a)+' ↗</a></div><div class="kpis">'+
  k.map(r=>'<div class="kpi"><div class="k">'+r[0]+'</div><div class="v">'+r[1]+'</div><div class="s">'+r[2]+'</div></div>').join('')+'</div><h3>'+(kind===1?'Payouts':'Block rewards')+' per UTC day, last 30 days</h3><div class="legend"><span class="lg"><i style="background:#731D30"></i>TSC</span></div></div>';
 const c=bars(daily,d.day0);R.querySelector('.mcard').appendChild(c);window.tscTip&&window.tscTip(c.querySelector('svg'));
 R.querySelector('.kpis').insertAdjacentHTML('beforebegin',health(d,kind,pool,last,est,daily,pm,first));
 R.querySelector('.mcard').insertAdjacentHTML('beforeend','<p class="note">'+(kind===1?'Pools pay in batches, so single days jump around; the 7-day average is the fairer number. Proof rate is grossed up by the pool fee.':'Block rewards at the epoch reward; luck makes daily numbers noisy.')+'</p>')}
F.addEventListener('submit',e=>{e.preventDefault();const a=document.getElementById('maddr').value.trim();if(a){history.replaceState(null,'','#'+a);show(a)}});
function fromHash(){const a=decodeURIComponent(location.hash.slice(1));if(a.startsWith('tc1')){document.getElementById('maddr').value=a;show(a);F.scrollIntoView({behavior:'smooth'})}}
window.addEventListener('hashchange',fromHash);fromHash()})();
"""

EPOCH_JS = r"""
document.querySelectorAll('main table').forEach(t=>{if(getComputedStyle(t.parentNode).overflowX==='visible'){const w=document.createElement('div');w.style.overflowX='auto';t.before(w);w.appendChild(t)}});
document.querySelectorAll('[data-left]').forEach(e=>{const t=+e.dataset.left;const tick=()=>{const s=t-Date.now()/1000;if(s<=0){e.textContent='any moment now';return}
 const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);e.textContent='in '+(d?d+' d ':'')+h+' h '+m+' min';};tick();setInterval(tick,30000)});
(function(){const S=document.getElementById('bench');if(!S)return;const P=+S.dataset.price,G=document.getElementById('bmgpu'),W=document.getElementById('bmwin'),I=document.getElementById('bmpoi');
 const f=(v,n)=>v.toLocaleString('en-US',{minimumFractionDigits:n,maximumFractionDigits:n});
 const set=p=>{S.querySelectorAll('.bmv').forEach(e=>e.textContent=f(e.dataset.y*p,3));if(P)S.querySelectorAll('.bmu').forEach(e=>e.textContent='$'+f(e.dataset.y*p*P,2))};
 G.addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;G.querySelectorAll('button').forEach(x=>x.classList.toggle('on',x===b));I.value='';set(+b.dataset.poi)});
 I.addEventListener('input',()=>{const v=parseFloat(I.value);if(!(v>0))return;G.querySelectorAll('button').forEach(x=>x.classList.remove('on'));set(v)});
 W.addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;W.querySelectorAll('button').forEach(x=>x.classList.toggle('on',x===b));S.querySelectorAll('tbody[data-bw]').forEach(t=>t.hidden=t.dataset.bw!==b.dataset.w)});
})();
"""

CSS = """
.health{border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin:0 0 14px;background:#fff}
.health.ok{border-color:#CFE5D8;background:#F6FAF7}.health.warn{border-color:#EFDDBF;background:#FDF8F0}.health.bad{border-color:#F0CFCB;background:#FCF3F2}
.hh{display:flex;align-items:center;gap:10px;margin-bottom:8px;flex-wrap:wrap}.hh .note{margin-left:auto}
.hdot{width:10px;height:10px;border-radius:50%;background:#2F7A55}.health.warn .hdot{background:#B26A00}.health.bad .hdot{background:#B42318}
.pill.up{border-color:rgba(47,122,85,.35);color:#2F7A55}.hrow{display:flex;gap:10px;align-items:baseline;padding:5px 0;font-size:14px;border-top:1px solid rgba(15,17,21,.06)}.hrow .pill{flex:0 0 auto;min-width:52px;text-align:center}
[hidden]{display:none!important}
.look{display:flex;gap:8px;margin:6px 0 18px;flex-wrap:wrap}.look input{flex:1;min-width:220px;padding:12px 16px;border-radius:999px;border:1px solid var(--line);background:#fff;color:var(--ink);font:14px var(--mono)}
.look input:focus{outline:none;border-color:rgba(115,29,48,.5);box-shadow:0 0 0 3px var(--acc-tint)}.look input::placeholder{color:var(--soft)}
.look button{padding:12px 22px;border:1px solid rgba(115,29,48,.35);border-radius:999px;background:var(--acc-tint);color:var(--acc);font:12px var(--mono);text-transform:uppercase;letter-spacing:.1em;cursor:pointer}.look button:hover{background:rgba(115,29,48,.14)}
.mres .mcard{background:#fff;color:var(--ink);border:1px solid var(--line);border-radius:12px;padding:18px 20px;margin-bottom:18px}.mres .note{color:var(--mute)}
.mhead{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:12px;font-size:13px;word-break:break-all}
.small{font-size:11px;color:var(--mute)}tr.muted td{color:var(--mute)}
.epoch{display:flex;gap:20px;justify-content:space-between;flex-wrap:wrap}.ekpis{display:flex;gap:28px;flex-wrap:wrap}.ekpis .v{font-size:22px;font-weight:600;letter-spacing:-.02em}.ekpis .s{font-size:12px;color:var(--mute)}
.bar{height:8px;background:var(--bg);border-radius:99px;overflow:hidden;margin:14px 0 6px}.bar i{display:block;height:100%;background:var(--acc)}
.calc{display:grid;grid-template-columns:minmax(280px,1fr) 2fr;gap:16px;align-items:start}@media(max-width:900px){.calc{grid-template-columns:1fr}}
.calc label{display:block;font:10.5px/1.5 var(--mono);text-transform:uppercase;letter-spacing:.1em;color:var(--soft);margin:0 0 10px}.calc input,.calc select{display:block;width:100%;margin-top:4px;padding:9px 11px;border:1px solid var(--line);border-radius:8px;font:15px var(--sans);text-transform:none;letter-spacing:0;color:var(--ink);background:#fff}.calc input:focus,.calc select:focus{outline:none;border-color:rgba(115,29,48,.5);box-shadow:0 0 0 3px var(--acc-tint)}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:10px}.seg{display:inline-flex;gap:6px;margin:4px 0 14px}
.seg button{border:1px solid var(--line);background:#fff;padding:6px 13px;border-radius:999px;font:11px var(--mono);text-transform:uppercase;letter-spacing:.1em;color:var(--mute);cursor:pointer}.seg button.on{background:var(--acc-tint);border-color:rgba(115,29,48,.35);color:var(--acc)}
.res .v{font-size:22px}
.bmctl{display:flex;gap:6px 18px;flex-wrap:wrap;align-items:center;margin:4px 0 10px}.bmctl .seg{flex-wrap:wrap;margin:0}
.bmcus{font:10.5px var(--mono);text-transform:uppercase;letter-spacing:.1em;color:var(--soft);display:inline-flex;align-items:center;gap:8px}.bmcus input{width:86px;padding:6px 10px;border:1px solid var(--line);border-radius:999px;font:13px var(--sans);color:var(--ink);background:#fff}
.bmbar{position:relative;height:6px;background:var(--bg);border-radius:3px;margin-top:5px;min-width:120px}.bmbar i{display:block;height:100%;background:var(--acc);border-radius:3px}.bmbar u{position:absolute;top:-3px;width:2px;height:12px;background:var(--ink)}
tr.bmref td{background:var(--paper);color:var(--mute)}tr.bmref .bmbar i{background:var(--soft)}.bmna{font-size:12px;border:1px dashed var(--soft);border-radius:999px;padding:3px 10px;white-space:nowrap}
"""
