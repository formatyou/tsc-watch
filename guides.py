"""
tsc-watch — guides for miners.

1. Proof efficiency (intelligence multiplier "tax" per miner / pool)   → proof.html
2. Protocol upgrades: timeline, adoption, checklist                    → upgrades.html
3. Start mining: GPU compatibility + ready-to-paste setup              → start.html
"""
import json
import re
from collections import defaultdict

DAY = 86400
REJECT_NEAR_PIN = 175          # ≥175 of 256 near-certain steps → block rejected (proof v4 blog)

# Curated from the official Telegram channel and git.tensorcash.org releases (checked 30 Sep 2026).
# In production the list of releases would be pulled from the Gitea API; activation heights are
# parsed from release notes ("block 27,615", "height 24150") and reviewed by hand.
TIMELINE = [
    {"date": "2026-08-03", "ver": "v1.1.0", "kind": "mandatory", "height": 20440, "who": "everyone",
     "what": "DAA v2 difficulty algorithm. Mandatory mainnet upgrade before block 20,440."},
    {"date": "2026-08-13", "ver": "—", "kind": "event", "height": 22165, "who": "—",
     "what": "Epoch 6: block reward 92.664 → 55.5984 TSC."},
    {"date": "2026-08-18", "ver": "v1.1.1", "kind": "verifier", "height": None, "who": "verifiers, pools",
     "what": "Verifier fix: honest Qwen3.5 end-of-segment windows no longer read as RED."},
    {"date": "2026-08-27", "ver": "v1.1.2 → v1.1.2a", "kind": "mandatory", "height": 24150, "who": "block producers, nodes",
     "what": "Tighter proof validation; v1.1.2 withdrawn same day, replaced by v1.1.2a."},
    {"date": "2026-08-30", "ver": "v1.1.3", "kind": "optional", "height": None, "who": "miners, pools",
     "what": "Central VDF beacon, used automatically by the miner."},
    {"date": "2026-08-31", "ver": "v1.1.4", "kind": "mandatory", "height": 24735, "who": "pools, solo miners",
     "what": "Pad-slot zero binding (consensus). Pool participants unaffected."},
    {"date": "2026-09-01", "ver": "v1.1.4a", "kind": "mandatory", "height": 24870, "who": "block producers",
     "what": "StepBind (TIP-0003). Legacy proofs accepted under a grace rule until 8 Sep 23:59 UTC."},
    {"date": "2026-09-07", "ver": "v1.1.4b / c", "kind": "fix", "height": None, "who": "node operators",
     "what": "Stale Quick-verification verdicts no longer stall nodes; Windows builds."},
    {"date": "2026-09-08", "ver": "—", "kind": "deadline", "height": None, "who": "block producers",
     "what": "StepBind grace ends. 71 of the last 135 blocks still carried legacy proofs that day."},
    {"date": "2026-09-14", "ver": "v1.2.0 / a / b", "kind": "mandatory", "height": 26950, "who": "everyone",
     "what": "Proof v4 and the intelligence multiplier. Pricing starts at block 26,950 (moved from 26,925)."},
    {"date": "2026-09-18", "ver": "supr-meow 0.7.0", "kind": "miner", "height": None, "who": "suprnova / supr-meow users",
     "what": "Proof v4 support in the community miner. Older builds stop producing valid work at the v3 sunset."},
    {"date": "2026-09-20", "ver": "v1.2.1", "kind": "mandatory", "height": 27615, "who": "block producers",
     "what": "v3 sunset: proof v4 mandatory, AMBER/RED rules enforced, stricter verifier."},
    {"date": "2026-09-23", "ver": "v1.2.2", "kind": "recommended", "height": None, "who": "pools, verifiers",
     "what": "Faster pool Quick verification; experimental SGLang mining; security hardening."},
]

# GPUs: name, VRAM GB, architecture, generation ok?, published PoI/s (None = no public benchmark)
GPUS = [
    ("RTX 5090", 32, "Blackwell", True, 18.6), ("RTX 5080", 16, "Blackwell", True, None), ("RTX 5070 Ti", 16, "Blackwell", True, None),
    ("RTX 5070", 12, "Blackwell", True, None), ("RTX 4090", 24, "Ada", True, None), ("RTX 4080 / Super", 16, "Ada", True, None),
    ("RTX 4070 Ti Super", 16, "Ada", True, None), ("RTX 4070 / Ti", 12, "Ada", True, None), ("RTX 4060 Ti 16 GB", 16, "Ada", True, None),
    ("RTX 3090 / Ti", 24, "Ampere", True, None), ("RTX 3080 12 GB", 12, "Ampere", True, None), ("RTX 3080 10 GB", 10, "Ampere", True, None),
    ("RTX 3070 / Ti", 8, "Ampere", True, None), ("RTX 3060 12 GB", 12, "Ampere", True, None),
    ("RTX PRO 6000 Blackwell", 96, "Blackwell", True, 26.0), ("RTX A6000 / A40", 48, "Ampere", True, 9.5), ("RTX A5000", 24, "Ampere", True, None),
    ("L40S", 48, "Ada", True, None), ("A100 80 GB", 80, "Ampere", True, None), ("H100 / H200", 80, "Hopper", True, 40.0), ("B200", 180, "Blackwell", True, 56.0),
    ("RTX 2080 Ti", 11, "Turing", False, None), ("GTX 1080 Ti", 11, "Pascal", False, None), ("AMD Radeon (any)", 16, "AMD", False, None),
    ("Apple Silicon (M1–M4)", 0, "Apple", None, None),
]

POOLS = [
    {"id": "suprnova", "name": "suprnova (0% fee)", "stratum": "stratum+tcp://tsc.suprnova.cc:3310", "guide": "https://tsc.suprnova.cc/StartMining.html"},
    {"id": "tiger", "name": "Tiger Pool (3% fee)", "stratum": None, "guide": "https://tsc-miner.tiger-pool.com/",
     "note": "Tiger uses its own WebSocket broker (wss://tsc.tiger-pool.com:443/v1/ws) with a personal token from the miner portal."},
    {"id": "aria", "name": "AriaPool (1% fee)", "stratum": None, "guide": "https://pool.ariabrain.com/tsc.html",
     "note": "AriaPool uses a WebSocket endpoint (wss://pool.ariabrain.com/tsc-pool/ws); new wallets are validated via their Discord."},
    {"id": "bob", "name": "TensorCash.Pool / Bob Labs", "stratum": None, "guide": "https://pool.tensorcash.boblabs.eu/",
     "note": "Bob Labs ships its own Docker image and HiveOS flight sheet; PPLNS over the last 100k shares."},
]


V4_PRICING = 26950            # proof v4 pricing activation
V3_SUNSET = 27615


def _q(a, p):
    a = sorted(a)
    return a[int((len(a) - 1) * p)] if a else None


def compute_agg(rows, tip_h, tip_ts):
    """rows: (height, ts, miner, version, mult, late, profile, state, near_pin) for blocks ≥ V4_PRICING."""
    r3 = lambda x: round(x, 3)
    rows = sorted(rows)
    v4 = [r for r in rows if r[3] == 4 and r[4]]
    if not v4:
        return None
    mults = [r[4] for r in v4]
    by = defaultdict(list)
    for r in v4:
        by[r[2]].append(r)
    miners = []
    for a, lst in by.items():
        if len(lst) < 3:
            continue
        m = [r[4] for r in lst]
        avg = lambda xs: r3(sum(xs) / len(xs))
        w = [r[4] for r in lst if r[1] > tip_ts - 7 * DAY]
        miners.append([a, len(lst), avg(m), r3(_q(m, .5)), r3(_q(m, .9)), r3(max(m)),
                       avg([r[5] or 1 for r in lst]), avg([r[6] or 1 for r in lst]), avg([r[7] or 1 for r in lst]),
                       round(sum(r[8] or 0 for r in lst) / len(lst)), max(r[8] or 0 for r in lst), len(w), avg(w) if w else None])
    miners.sort(key=lambda x: -x[1])
    top = [m[0] for m in miners[:5]]
    days = defaultdict(lambda: {"n": 0, "v3": 0, "v4": 0, "m": 0.0, "pm": defaultdict(list)})
    for r in rows:
        e = days[r[1] // DAY * DAY]
        e["n"] += 1
        e["v3"] += r[3] == 3
        e["v4"] += r[3] == 4
        e["m"] += r[4] or 1
        if r[3] == 4 and r[2] in top:
            e["pm"][top.index(r[2])].append(r[4])
    daily = [[d, e["n"], e["v3"], e["v4"], r3(e["m"] / e["n"]),
              [r3(sum(e["pm"][i]) / len(e["pm"][i])) if e["pm"].get(i) else None for i in range(len(top))]]
             for d, e in sorted(days.items())]
    buckets = []
    for h in range(V4_PRICING, V3_SUNSET + 100, 25):
        b = [r for r in rows if h <= r[0] < h + 25]
        if b:
            buckets.append([h, len(b), sum(1 for r in b if r[3] == 4), b[0][1]])
    edges = [(1, 1.001), (1.001, 1.05), (1.05, 1.1), (1.1, 1.2), (1.2, 1.3), (1.3, 1.5), (1.5, 2), (2, 13)]
    hist = [[a, b, sum(1 for x in mults if a <= x < b)] for a, b in edges]
    return {"tip": tip_h, "tip_ts": tip_ts, "daily": daily, "top": top, "miners": miners, "buckets": buckets, "hist": hist,
            "all": {"n": len(v4), "mean": r3(sum(mults) / len(mults)), "p50": r3(_q(mults, .5)), "p90": r3(_q(mults, .9))}}


HEIGHT_RE = re.compile(r"(?:block|height)\s*(?:height\s*)?#?\s*(\d{1,3}(?:,\d{3})+|\d{5,7})", re.I)


def parse_releases(raw):
    """Gitea releases → [(tag, date, name, [activation heights])]."""
    out = []
    for r in raw or []:
        hs = sorted({int(x.replace(",", "")) for x in HEIGHT_RE.findall(r.get("body") or "")})
        out.append({"tag": r.get("tag"), "date": (r.get("date") or "")[:10], "name": r.get("name") or r.get("tag"), "heights": hs})
    out.sort(key=lambda r: r["date"], reverse=True)
    return out


def _dur(sec):
    return f"{sec / 3600:.0f} h" if sec < 48 * 3600 else f"{sec / 86400:.1f} days"


def _name(aliases, a):
    return aliases.get(a) or (a[:10] + "…" + a[-6:])


# ---------------------------------------------------------------- 1. proof efficiency

def proof_page(B, agg, aliases, price, reward):
    esc, fnum, fusd = B.esc, B.fnum, B.fusd
    al = agg["all"]
    at1 = next((h[2] for h in agg["hist"] if h[0] == 1), 0)
    rows, lost_total = [], 0.0
    for m in agg["miners"]:
        a, n, mean, p50, p90, mx, late, prof, state, npin, npmax, n7, mean7 = m
        extra = (mean - 1) * 100
        lost = n * (mean - 1)
        lost_total += lost
        drivers = sorted((("late credit", late), ("sampling profile", prof), ("state", state)), key=lambda x: -x[1])
        main = drivers[0]
        if mean <= 1.02:
            diag, cls = "efficient", "up"
        elif main[1] > 1.02:
            diag, cls = main[0], ("down" if mean > 1.15 else "warn")
        else:
            diag, cls = "occasional spikes", "warn"
        risk = f"{npmax} / {REJECT_NEAR_PIN}"
        rows.append(f"<tr><td><a class='mono' href='miner.html#{esc(a)}'>{esc(_name(aliases, a))}</a></td><td>{fnum(n)}</td>"
                    f"<td><b>{fnum(mean, 3)}×</b></td><td>{fnum(p90, 3)}×</td><td class='{cls}'>+{fnum(extra, 1)}%</td>"
                    f"<td>{fnum(lost, 1)}</td><td>{fusd(lost * reward * price, 0) if price else '—'}</td>"
                    f"<td>{fnum(late, 3)} · {fnum(prof, 3)} · {fnum(state, 3)}</td><td>{risk}</td><td><span class='pill {cls}'>{esc(diag)}</span></td></tr>")

    hist = [(i, h[2]) for i, h in enumerate(agg["hist"])]
    labels = ["1.00×", "1.00–1.05", "1.05–1.10", "1.10–1.20", "1.20–1.30", "1.30–1.50", "1.50–2.00", "≥ 2.00"]
    hist_svg = _hist(B, [h[1] for h in hist], labels, al["n"])

    top = agg["top"]
    series = []
    for k, a in enumerate(top):
        pts = [(d[0], d[5][k]) for d in agg["daily"] if d[5][k] is not None]
        if len(pts) >= 3:
            series.append({"name": _name(aliases, a), "points": pts, "area": False, "width": 2, "color": B.PALETTE[k]})
    series.append({"name": "ideal 1.00×", "points": [(agg["daily"][0][0], 1), (agg["daily"][-1][0], 1)], "color": "#969BA1", "dash": True, "area": False})
    trend_svg = B.svg_line(series, yfmt=lambda v: fnum(v, 2) + "×", y0=False, tipfmt=lambda v: fnum(v, 3) + "×")

    return f"""<section class="hero">
<h1>Proof <em>efficiency</em>: the intelligence-multiplier tax.</h1>
<p class="lead">Since proof v4 every block carries a multiplier from 1× up to 12×. A 1.2× block needs 20% more work than a 1.0× block. Miners and pools that land near 1.0× find more blocks with the same GPUs. This page shows who pays the tax and why.</p>
<div class="grid">
<div class="cell"><div class="k">Network mean, since v4</div><div class="v">{fnum(al['mean'], 3)}×</div><div class="s">median {fnum(al['p50'], 3)}× · 90% of blocks ≤ {fnum(al['p90'], 3)}× · {fnum(al['n'])} blocks</div></div>
<div class="cell"><div class="k">Blocks priced at exactly 1.0×</div><div class="v">{fnum(at1 / al['n'] * 100, 0)}%</div><div class="s">{fnum(at1)} of {fnum(al['n'])} v4 blocks</div></div>
<div class="cell"><div class="k">Work spent on the tax</div><div class="v">{fnum(lost_total, 0)} blocks</div><div class="s">≈ {fnum(lost_total * reward, 0)} TSC ≈ {fusd(lost_total * reward * price, 0) if price else '—'} at today's price</div></div>
<div class="cell"><div class="k">Rejection line</div><div class="v">{REJECT_NEAR_PIN} / 256</div><div class="s">near-certain steps; highest seen: {max(m[10] for m in agg['miners'])}</div></div>
</div></section>
<section class="card"><h2>Who pays the tax</h2><p class="sub">Block finders with ≥3 proof-v4 blocks. "Extra work" = mean multiplier − 1. "Blocks lost" = blocks they would have found at 1.0× with the same work.</p>
<div style="overflow:auto"><table><thead><tr><th>Miner / pool</th><th>v4 blocks</th><th>Mean</th><th>p90</th><th>Extra work</th><th>Blocks lost</th><th>Value lost</th><th>Late · profile · state</th><th>Max near-pin</th><th>Main driver</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<p class="note">Components are the four factors of the v4 price (the "pin" factor was 1.0 on every block). Pool miners inherit their pool's multiplier: a pool at 1.12× pays everyone ~12% less than the same pool at 1.0× would.</p></section>
<section class="card"><h2>Daily mean multiplier, largest producers</h2><p class="sub">Lower is better. Dashed line = no tax.</p>{trend_svg}</section>
<section class="card"><h2>Distribution</h2><p class="sub">All proof-v4 blocks by multiplier.</p>{hist_svg}</section>
<section class="card"><h2>What moves the multiplier</h2><dl>
<dt>Late credit</dt><dd>Credit that piles up at the end of the 256-step window or in its last 32–64 steps. Possible causes: custom samplers, unusual prompts, outdated builds. Worth comparing against the reference miner.</dd>
<dt>Sampling profile</dt><dd>How the proof's token-selection behaviour compares with ordinary inference. Priced above 1.0× when it looks optimised for mining rather than normal generation.</dd>
<dt>Near-certain steps</dt><dd>Steps where the model is almost sure of the next token. 175 of 256 rejects the block outright.</dd>
<dt>Reference</dt><dd>The project's honest prompt bank: median 1.07×, 91% of windows ≤ 1.3×.</dd></dl>
<p class="note">Source: block headers from the explorer (proof.multiplier, proof.components, proof.near_pin_count), method from the proof-v4 blog post.</p></section>"""


def _hist(B, vals, labels, total, width=900, height=240):
    pl, pr, pt, pb = 44, 10, 14, 34
    W, H = width - pl - pr, height - pt - pb
    mx = max(vals) * 1.1 or 1
    bw = W / len(vals)
    out = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img">']
    for t in B._ticks(0, mx):
        y = pt + H - t / mx * H
        out.append(f'<line x1="{pl}" x2="{width-pr}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/><text x="{pl-6}" y="{y+4:.1f}" class="tick" text-anchor="end">{B.fnum(t, 0)}</text>')
    for i, v in enumerate(vals):
        x, y = pl + i * bw, pt + H - v / mx * H
        col = B.PALETTE[2] if i == 0 else (B.PALETTE[0] if i < 4 else "#B42318")
        out.append(f'<rect x="{x+bw*.12:.1f}" y="{y:.1f}" width="{bw*.76:.1f}" height="{pt+H-y:.1f}" fill="{col}" opacity=".85"><title>{labels[i]}: {v} blocks ({v/total*100:.1f}%)</title></rect>')
        out.append(f'<text x="{x+bw/2:.1f}" y="{y-4:.1f}" class="tick" text-anchor="middle">{v/total*100:.0f}%</text>')
        out.append(f'<text x="{x+bw/2:.1f}" y="{height-12}" class="tick" text-anchor="middle">{labels[i].replace("–", "–")}</text>')
    out.append("</svg>")
    return '<div class="legend"><span class="lg"><i style="background:#2F7A55"></i>1.00× (no tax)</span><span class="lg"><i style="background:#731D30"></i>up to 1.2×</span><span class="lg"><i style="background:#B42318"></i>above 1.2×</span></div>' + "".join(out)


# ---------------------------------------------------------------- 2. upgrades

def upgrades_page(B, agg, tip_h, tip_ts, bt, releases=None):
    esc, fnum = B.esc, B.fnum
    releases = releases or []
    known = {t["height"] for t in TIMELINE if t["height"]}
    extra = [{"date": r["date"], "ver": r["tag"], "kind": "announced", "height": h, "who": "see release notes", "what": r["name"]}
             for r in releases for h in r["heights"] if h > tip_h and h not in known]
    pending = sorted([t for t in TIMELINE + extra if t["height"] and t["height"] > tip_h], key=lambda t: t["height"])
    nxt = pending[0] if pending else None
    latest = releases[0] if releases else None
    new_rel = [r for r in releases if r["date"] > max(t["date"] for t in TIMELINE)]
    kinds = {"mandatory": "down", "deadline": "down", "fix": "warn", "verifier": "warn", "recommended": "", "optional": "", "miner": "warn", "event": "", "announced": "down", "new release": "warn"}
    rows = []
    for t in sorted(TIMELINE + extra + [{"date": r["date"], "ver": r["tag"], "kind": "new release", "height": None, "who": "see release notes", "what": r["name"]} for r in new_rel],
                    key=lambda t: t["date"], reverse=True):
        h = f"#{fnum(t['height'])}" if t["height"] else "—"
        state = ""
        if t["height"]:
            state = "active" if tip_h >= t["height"] else f"in {fnum(t['height'] - tip_h)} blocks"
        rows.append(f"<tr><td class='mono'>{esc(t['date'])}</td><td><b>{esc(t['ver'])}</b></td><td><span class='pill {kinds.get(t['kind'], '')}'>{esc(t['kind'])}</span></td>"
                    f"<td class='mono'>{h}</td><td>{esc(state)}</td><td>{esc(t['who'])}</td><td class='wrap'>{esc(t['what'])}</td></tr>")
    pts = [(b[0], b[2] / b[1] * 100) for b in agg["buckets"]]
    adopt = B.svg_line([{"name": "share of blocks with proof v4, per 25 blocks", "points": pts, "width": 2}],
                       yfmt=lambda v: fnum(v, 0) + "%", xfmt=lambda h: "#" + fnum(h), markers=[(26950, "pricing starts"), (27615, "v3 sunset")],
                       tipfmt=lambda v: fnum(v, 0) + "%")
    half = next((b for b in agg["buckets"] if b[2] / b[1] >= 0.5), None)
    first_half = half[0] if half else None
    half_h = (half[3] - agg["buckets"][0][3]) / 3600 if half and len(half) > 3 else None
    mandatory = [t for t in TIMELINE if t["kind"] == "mandatory"]
    return f"""<section class="hero">
<h1>Protocol <em>upgrades</em> and deadlines.</h1>
<p class="lead">TensorCash changes fast: {len(mandatory)} mandatory upgrades in two months. Miners on an old build keep their GPUs busy but their work stops counting. One page with every activation height, who must act and how far the network has moved.</p>
<div class="grid">
<div class="cell"><div class="k">Next activation</div><div class="v">{('#' + fnum(nxt['height'])) if nxt else 'none pending'}</div><div class="s">{esc(nxt['ver'] + ' · ' + nxt['what']) + ' · in ' + fnum(nxt['height'] - tip_h) + ' blocks ≈ ' + _dur((nxt['height'] - tip_h) * bt) if nxt else 'current rules: proof v4, v1.2.x'}</div></div>
<div class="cell"><div class="k">Latest release</div><div class="v">{esc(latest['tag']) if latest else 'v1.2.2'}</div><div class="s">{esc(latest['date']) if latest else '2026-09-23'} · git.tensorcash.org · {fnum(len(releases)) if releases else '17'} releases listed</div></div>
<div class="cell"><div class="k">Proof v4 reached 50% at</div><div class="v">#{fnum(first_half) if first_half else '—'}</div><div class="s">≈ {fnum(half_h, 0) if half_h is not None else '—'} h after pricing started; 100% only at the sunset block</div></div>
<div class="cell"><div class="k">Current tip</div><div class="v">#{fnum(tip_h)}</div><div class="s">{B.fdt(tip_ts)}</div></div>
</div></section>
<section class="card"><h2>How fast the network upgraded to proof v4</h2><p class="sub">Most producers switched only in the last hours before the v3 sunset. An alert 24 h and 2 h before each activation would have saved work.</p>{adopt}</section>
<section class="card"><h2>Timeline</h2><p class="sub">Newest first. "Block producers" = pool operators and solo miners; pool participants follow their pool.</p>
<div style="overflow:auto"><table><thead><tr><th>Date</th><th>Version</th><th>Type</th><th>Activation</th><th>Status</th><th>Who must act</th><th>What changes</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div></section>
<section class="card"><h2>Upgrade checklist</h2><div class="two"><div><h3>Pool miner</h3><ol class="check">
<li>Watch your pool's announcement: it upgrades first.</li><li>Update the miner to the version your pool names (supr-meow ≥ 0.7.0 for proof v4).</li>
<li>Keep the model cache; the first start after an upgrade may re-download.</li><li>Check your address on the <a href="miner.html">miner dashboard</a> the next day: payouts should continue.</li></ol></div>
<div><h3>Solo miner / pool operator</h3><ol class="check"><li>Upgrade bcore, TensorCash and your verifier before the activation height, not the estimated time.</li>
<li>Verify release signatures at verify.tensorcash.org.</li><li>After activation, confirm your next block shows the new proof version.</li><li>Rejected blocks leave no trace on-chain: watch your own node logs.</li></ol></div></div></section>"""


# ---------------------------------------------------------------- 3. start mining

def start_page(B, net_rate, new_day, price):
    params = {"gpus": [{"n": g[0], "vram": g[1], "arch": g[2], "ok": g[3], "poi": g[4]} for g in GPUS], "pools": POOLS,
              "net_rate": net_rate, "new_day": new_day, "price": price}
    opts = "".join(f'<option value="{i}">{B.esc(g[0])}{"" if not g[1] else f" · {g[1]} GB"}</option>' for i, g in enumerate(GPUS))
    popts = "".join(f'<option value="{p["id"]}">{B.esc(p["name"])}</option>' for p in POOLS)
    return f"""<section class="hero">
<h1>Start <em>mining</em> in five minutes.</h1>
<p class="lead">Pick your GPU, rig software and pool. You get a straight answer on whether the card can mine, what it should earn and the exact configuration to paste. No coding agent needed.</p></section>
<div class="calc"><section class="card"><h2>Your rig</h2>
<label>GPU<select id="s_gpu">{opts}</select></label>
<div class="row2"><label>Number of GPUs<input id="s_n" type="number" min="1" value="1"></label><label>Rig software<select id="s_os"><option value="hive">HiveOS</option><option value="mmpos">MMPOS</option><option value="smos">SimpleMining</option><option value="linux" selected>Linux (bare metal)</option><option value="win">Windows 10/11</option><option value="docker">Docker</option></select></label></div>
<label>Pool<select id="s_pool">{popts}</select></label>
<label>Your TSC address<input id="s_addr" placeholder="tc1q…" spellcheck="false"></label>
<label>Worker name<input id="s_worker" value="rig1"></label></section>
<section class="card"><h2>Result</h2><div id="s_verdict"></div><div class="kpis res" id="s_est"></div><h3>Configuration</h3><pre id="s_cfg" class="code"></pre>
<p class="note" id="s_note"></p>
<h3>Units, in plain words</h3><p class="note">Explorers show PoI/s, supr-meow prints windows/s, suprnova also shows tok/s. For planning: 1 window/s ≈ 1 PoI/s (RTX 5090: 18.7 windows/s in supr-meow vs 18.6 PoI/s in suprnova's table), and suprnova converts 1 PoI/s ≈ 256 tokens/s.</p></section></div>
<script>window.TSC_START={json.dumps(params)}</script>"""


START_JS = r"""
(function(){const P=window.TSC_START;if(!P)return;const $=id=>document.getElementById(id);
const f2=(x,d=2)=>x==null||!isFinite(x)?'—':x.toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d});
function run(){const g=P.gpus[+$('s_gpu').value],n=Math.max(1,+$('s_n').value||1),os=$('s_os').value,pool=P.pools.find(p=>p.id===$('s_pool').value);
 const addr=$('s_addr').value.trim(),worker=$('s_worker').value.trim()||'rig1';let v='',cls='up',mode='single',engine='supr-meow (llama.cpp)';
 if(g.arch==='Apple'){v='Apple Silicon can mine with the official TensorMiner app (Metal). supr-meow does not support Apple GPUs.';cls='warn';mode='apple'}
 else if(g.ok===false){v=g.arch+' cannot mine TensorCash: '+(g.arch==='Turing'||g.arch==='Pascal'?'no bf16 support (Ampere or newer required).':'only NVIDIA GPUs are supported.');cls='down';mode='no'}
 else if(g.vram>=12){v='Supported. One model per card ('+g.vram+' GB ≥ 12 GB).';if(g.vram<24)v+=' More memory can improve speed.'}
 else if(g.vram>=8){if(n>=2&&n%2===0){v='Supported as matched pairs: two identical '+g.vram+' GB cards share one model (SPLIT_MODEL=1).';mode='pair'}else{v='A single '+g.vram+' GB card is too small. Two identical cards can share a model.';cls='warn';mode='no'}}
 else{v='Too little memory.';cls='down';mode='no'}
 $('s_verdict').innerHTML='<p class="verdict '+cls+'">'+v+'</p>'+(addr&&!/^tc1q[0-9a-z]{30,60}$/.test(addr)?'<p class="verdict down">That does not look like a TSC address (tc1q…).</p>':'');
 let est='';if(g.poi&&mode!=='no'){const r=g.poi*n,tsc=P.new_day*r/P.net_rate;est=[['Published rate',f2(r,1)+' PoI/s',n+' × '+g.n],['TSC per day',f2(tsc,3),'before pool fee, at today\'s network'],['USD per day','$'+f2(tsc*P.price),'at $'+f2(P.price,3)]]}
 else if(mode==='pair'){const r=2.5*n,tsc=P.new_day*r/P.net_rate;est=[['Expected rate','~'+f2(r,1)+' PoI/s','2 × RTX 3070 ≈ 5.0 per pair (supr-meow 0.6.0)'],['TSC per day',f2(tsc,3),'rough'],['USD per day','$'+f2(tsc*P.price),'rough']]}
 else if(mode!=='no'&&mode!=='apple'){est=[['Rate','no public benchmark','measure with a 10-minute run, then use the calculator']]}
 $('s_est').innerHTML=est?est.map(r=>'<div class="kpi"><div class="k">'+r[0]+'</div><div class="v">'+r[1]+'</div><div class="s">'+(r[2]||'')+'</div></div>').join(''):'';
 const W=(addr||'tc1qYOUR_ADDRESS')+'.'+worker,dev=Array.from({length:n},(_,i)=>i).join(','),split=mode==='pair';let cfg='',note='';
 if(mode==='no'||mode==='apple'){cfg=mode==='apple'?'Download TensorMiner for macOS: https://tensorcash.org/blog/how-to-mine/':'—'}
 else if(pool.stratum){const url=pool.stratum;
  if(os==='hive'||os==='mmpos'||os==='smos'){const pkg={hive:'supr-meow-tsc-0.7.0.tar.gz',mmpos:'supr-meow-tsc-0.7.0-mmpos.tar.gz',smos:'supr-meow-tsc-0.7.0-smos.tar.gz'}[os];
   cfg='# Flight sheet / custom miner\nInstallation URL: https://github.com/ocminer/supr-meow-tsc/releases/download/v0.7.0/'+pkg+'\nPool URL:         '+url+'\nWallet:           '+W+'\n# Extra config\nDEVICES='+dev+(split?'\nSPLIT_MODEL=1':'');note=os==='hive'?'HiveOS: use the archive name exactly as shown (no renaming).':''}
  else if(os==='docker'){cfg='docker run -d --restart unless-stopped --gpus all \\\n  -v meow-models:/models \\\n  -e POOL_URL='+url+' \\\n  -e WALLET='+W+' -e DEVICES='+dev+(split?' -e SPLIT_MODEL=1':'')+' \\\n  ocminersupr/supr-meow-tsc:0.7.0'}
  else if(os==='win'){cfg='REM supr-meow-tsc-0.7.0-windows-x86_64.zip, then in its folder:\nset POOL_URL='+url+'\nset WALLET='+W+'\nset DEVICES='+dev+(split?'\nset SPLIT_MODEL=1':'')+'\nstart.bat';note='Windows package is new in 0.7.0; check the bundled README for the launcher name.'}
  else{cfg="tar xzf supr-meow-tsc-0.7.0-linux-x86_64.tar.gz && cd supr-meow-tsc-0.7.0\nPOOL_URL='"+url+"' \\\nWALLET='"+W+"' DEVICES="+dev+(split?' SPLIT_MODEL=1':'')+" ./meow-common.sh"}
  note=(note?note+' ':'')+'First start downloads the model (≥10 GB); keep the cache between runs. Verify the .sha256 checksum from the release page.'}
 else{cfg='# '+pool.name+'\n# '+pool.note+'\n# Setup guide: '+pool.guide;note='This pool does not use a public stratum URL; follow its own guide.'}
 $('s_cfg').textContent=cfg;$('s_note').textContent=note}
['s_gpu','s_n','s_os','s_pool','s_addr','s_worker'].forEach(i=>$(i).addEventListener('input',run));run()})();
"""

CSS = """
.labtag{display:inline-block;margin-bottom:12px;padding:4px 12px;border-radius:999px;border:1px dashed rgba(115,29,48,.5);font:10.5px var(--mono);text-transform:uppercase;letter-spacing:.12em;color:var(--acc);background:#fff}
nav a.lab{border:1px dashed rgba(115,29,48,.4)}
td.wrap{white-space:normal;min-width:240px;text-align:left}
.pill{display:inline-block;padding:2px 9px;border-radius:999px;font:10px var(--mono);text-transform:uppercase;letter-spacing:.08em;border:1px solid var(--line);color:var(--mute)}
.pill.down{border-color:rgba(180,35,24,.35);color:#B42318}.pill.warn{border-color:rgba(178,106,0,.35);color:#B26A00}
.verdict{padding:12px 14px;border-radius:10px;border:1px solid var(--line);margin:0 0 12px;font-weight:500}.verdict.up{background:#F1F7F3;border-color:#CFE5D8}.verdict.down{background:#FCF1F0;border-color:#F0CFCB}.verdict.warn{background:#FDF6EC;border-color:#EFDDBF}
.calc>*{min-width:0}pre.code{background:#0F1115;color:#E8EAED;border-radius:10px;padding:14px 16px;font:12.5px/1.6 var(--mono);overflow:auto;white-space:pre}
ol.check{padding-left:20px;margin:0}ol.check li{margin:0 0 6px}
.health{margin:0 0 14px}
"""
