#!/usr/bin/env python3
"""
tsc-watch — static dashboard generator: data/tsc.db → site/.
Standard library only; charts are inline SVG.
"""
import html
import json
import os
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

import costs
import i18n
import miners
import guides

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT, "data", "tsc.db")
SITE = os.path.join(ROOT, "site")
SATS = 100_000_000
DAY = 86400

# reward epochs (fallback when snapshots are missing); [start_height, end_height, reward_TSC]
EPOCHS_FALLBACK = [(0, 715, 715.0), (715, 2145, 429.0), (2145, 5005, 257.4), (5005, 10725, 154.44),
                   (10725, 22165, 92.664), (22165, 45045, 55.5984)]


# ---------------------------------------------------------------- formatting (EN, UTC)

def fnum(x, d=0):
    if x is None:
        return "—"
    return f"{x:,.{d}f}"


def fshort(x, d=1):
    """1234567 → 1.23M ; 12345 → 12.3K"""
    if x is None:
        return "—"
    a = abs(x)
    if a >= 1e9:
        return fnum(x / 1e9, 2) + "B"
    if a >= 1e6:
        return fnum(x / 1e6, 2) + "M"
    if a >= 1e3:
        return fnum(x / 1e3, d) + "K"
    return fnum(x, d if a < 100 else 0)


def fusd(x, d=None):
    if x is None:
        return "—"
    if d is None:
        d = 4 if abs(x) < 10 else (2 if abs(x) < 1000 else 0)
    return "$" + fnum(x, d)


def fpct(x, d=1, sign=True):
    if x is None:
        return "—"
    s = fnum(x, d) + "%"
    if sign and x > 0:
        s = "+" + s
    return s


def frate(x):
    """proof/s → 'K proof/s'"""
    if x is None:
        return "—"
    if x >= 1e6:
        return fnum(x / 1e6, 2) + " M proof/s"
    if x >= 1e3:
        return fnum(x / 1e3, 1) + " K proof/s"
    return fnum(x, 0) + " proof/s"


def fdt(ts, fmt="%d %b %Y, %H:%M UTC"):
    return datetime.fromtimestamp(ts, timezone.utc).strftime(fmt) if ts else "—"


def fday(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%d %b")


def fdur(sec):
    if sec is None:
        return "—"
    if sec < 90:
        return f"{sec:.0f} s"
    if sec < 3600:
        return f"{sec/60:.1f} min"
    return f"{sec/3600:.1f} h"


def short_addr(a):
    return a[:8] + "…" + a[-6:] if a and len(a) > 20 else (a or "—")


def esc(s):
    return html.escape(str(s))


def signed(x, d=0):
    if x is None:
        return "—"
    return ("+" if x > 0 else "") + fnum(x, d)


# ---------------------------------------------------------------- SVG charts

PALETTE = ["#731D30", "#C07A2C", "#2F7A55", "#8A5A9E", "#3F6FA0", "#1F8A8A", "#969BA1"]


def _ticks(lo, hi, n=4):
    if hi <= lo:
        hi = lo + 1
    import math
    span = hi - lo
    raw = span / n
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        step = m * mag
        if span / step <= n + 1:
            break
    start = math.floor(lo / step) * step
    t = []
    v = start
    while v <= hi + step * 0.001:
        if v >= lo - step * 0.001:
            t.append(v)
        v += step
    return t


def svg_line(series, width=900, height=260, yfmt=fshort, xfmt=fday, area=True, y0=True, markers=None, tipfmt=None):
    """series: list of dict(name, points=[(x,y)], color, width, dash, area, scale). Shared X axis (unix ts)."""
    pad_l, pad_r, pad_t, pad_b = 58, 16, 14, 30
    pts_all = [p for s in series for p in s["points"] if p[1] is not None]
    if not pts_all:
        return '<div class="empty">No data yet</div>'
    xs = [p[0] for p in pts_all]
    scale_series = [s for s in series if s.get("scale")] or series
    ys = [p[1] for s in scale_series for p in s["points"] if p[1] is not None] or [p[1] for p in pts_all]
    x0, x1 = min(xs), max(xs)
    ylo = 0 if y0 else min(ys)
    yhi = max(ys)
    if yhi == ylo:
        yhi = ylo + 1
    yhi *= 1.06
    W = width - pad_l - pad_r
    H = height - pad_t - pad_b

    def X(x):
        return pad_l + (x - x0) / max(1, (x1 - x0)) * W

    def Y(y):
        y = min(max(y, ylo), yhi)
        return pad_t + H - (y - ylo) / (yhi - ylo) * H

    tf = tipfmt or yfmt
    tip_s = []
    for k, s in enumerate(series):
        raw = sorted((x, y) for x, y in s["points"] if y is not None)
        if not raw:
            continue
        step = max(1, -(-len(raw) // 700))
        keep = raw[::step] + ([raw[-1]] if (len(raw) - 1) % step else [])
        tip_s.append([esc(s.get("name", "")), s.get("color", PALETTE[k % len(PALETTE)]),
                      [[round(X(x), 1), round(Y(y), 1), int(x), tf(y)] for x, y in keep]])
    tip = {"d": int(all(x % DAY == 0 for x in xs)), "b": [pad_t, pad_t + H], "l": pad_l, "r": width - pad_r, "s": tip_s}
    out = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" data-tip="{esc(json.dumps(tip, separators=(",", ":")))}">']
    for t in _ticks(ylo, yhi):
        y = Y(t)
        out.append(f'<line x1="{pad_l}" x2="{width-pad_r}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>')
        out.append(f'<text x="{pad_l-6}" y="{y+4:.1f}" class="tick" text-anchor="end">{esc(yfmt(t))}</text>')
    n = 6
    for i in range(n + 1):
        x = x0 + (x1 - x0) * i / n
        out.append(f'<text x="{X(x):.1f}" y="{height-8}" class="tick" text-anchor="middle">{esc(xfmt(x))}</text>')
    for k, s in enumerate(series):
        pts = [(X(x), Y(y)) for x, y in s["points"] if y is not None]
        if len(pts) < 2:
            continue
        col = s.get("color", PALETTE[k % len(PALETTE)])
        d = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        if area and s.get("area", k == 0):
            out.append(f'<polygon points="{pts[0][0]:.1f},{pad_t+H} {d} {pts[-1][0]:.1f},{pad_t+H}" fill="{col}" opacity="0.10"/>')
        dash = ' stroke-dasharray="4 3"' if s.get("dash") else ""
        out.append(f'<polyline points="{d}" fill="none" stroke="{col}" stroke-width="{s.get("width",1.8)}" opacity="{s.get("opacity",1)}"{dash} stroke-linejoin="round"/>')
    for m in markers or []:
        x = X(m[0])
        out.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{pad_t}" y2="{pad_t+H}" class="marker"/>')
        anchor = "end" if x > pad_l + W * 0.8 else "start"
        out.append(f'<text x="{x + (-4 if anchor == "end" else 4):.1f}" y="{pad_t+12}" class="mlabel" text-anchor="{anchor}">{esc(m[1])}</text>')
    out.append("</svg>")
    legend = "".join(f'<span class="lg"><i style="background:{s.get("color", PALETTE[k % len(PALETTE)])}"></i>{esc(s["name"])}</span>'
                     for k, s in enumerate(series) if s.get("name"))
    return f'<div class="legend">{legend}</div><div class="cw">' + "".join(out) + "</div>"


def svg_bars(points, width=900, height=220, yfmt=fshort, xfmt=fday, color=PALETTE[0], name="daily value"):
    pad_l, pad_r, pad_t, pad_b = 58, 16, 14, 30
    if not points:
        return '<div class="empty">No data yet</div>'
    ys = [p[1] for p in points]
    yhi = max(ys) * 1.06 or 1
    W = width - pad_l - pad_r
    H = height - pad_t - pad_b
    n = len(points)
    bw = W / n

    def Y(y):
        return pad_t + H - y / yhi * H

    tip = {"d": int(all(p[0] % DAY == 0 for p in points)), "b": [pad_t, pad_t + H], "l": pad_l, "r": width - pad_r, "bw": round(bw, 1),
           "bars": [[round(pad_l + i * bw + bw / 2, 1), int(x), [[esc(name), color, yfmt(y)]]] for i, (x, y) in enumerate(points)]}
    out = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" data-tip="{esc(json.dumps(tip, separators=(",", ":")))}">']
    for t in _ticks(0, yhi):
        y = Y(t)
        out.append(f'<line x1="{pad_l}" x2="{width-pad_r}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>')
        out.append(f'<text x="{pad_l-6}" y="{y+4:.1f}" class="tick" text-anchor="end">{esc(yfmt(t))}</text>')
    step = max(1, n // 6)
    for i, (x, y) in enumerate(points):
        xx = pad_l + i * bw
        out.append(f'<rect x="{xx+bw*0.12:.1f}" y="{Y(y):.1f}" width="{bw*0.76:.1f}" height="{pad_t+H-Y(y):.1f}" fill="{color}" opacity="0.85"/>')
        if i % step == 0:
            out.append(f'<text x="{xx+bw/2:.1f}" y="{height-8}" class="tick" text-anchor="middle">{esc(xfmt(x))}</text>')
    out.append("</svg>")
    return f'<div class="legend"><span class="lg"><i style="background:{color}"></i>{esc(name)}</span></div><div class="cw">' + "".join(out) + "</div>"


def svg_stacked(days, keys, labels, colors, width=900, height=240, xfmt=fday, line50=True):
    """days: list of (ts, {key: share_pct}) — 100% stacked bars."""
    pad_l, pad_r, pad_t, pad_b = 44, 16, 14, 30
    if not days:
        return '<div class="empty">No data yet</div>'
    W = width - pad_l - pad_r
    H = height - pad_t - pad_b
    n = len(days)
    bw = W / n
    tip = {"d": int(all(ts % DAY == 0 for ts, _ in days)), "b": [pad_t, pad_t + H], "l": pad_l, "r": width - pad_r, "bw": round(bw, 1),
           "bars": [[round(pad_l + i * bw + bw / 2, 1), int(ts),
                     [[esc(labels[k]), colors[k], fnum(sh.get(key, 0.0), 1) + "%"] for k, key in enumerate(keys) if sh.get(key, 0.0) > 0]]
                    for i, (ts, sh) in enumerate(days)]}
    out = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" data-tip="{esc(json.dumps(tip, separators=(",", ":")))}">']
    for t in (0, 25, 50, 75, 100):
        y = pad_t + H - t / 100 * H
        out.append(f'<line x1="{pad_l}" x2="{width-pad_r}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>')
        out.append(f'<text x="{pad_l-6}" y="{y+4:.1f}" class="tick" text-anchor="end">{t}%</text>')
    step = max(1, n // 7)
    for i, (ts, shares) in enumerate(days):
        xx = pad_l + i * bw
        acc = 0.0
        for k, key in enumerate(keys):
            v = shares.get(key, 0.0)
            if v <= 0:
                continue
            y1 = pad_t + H - (acc + v) / 100 * H
            hgt = v / 100 * H
            out.append(f'<rect x="{xx+bw*0.08:.1f}" y="{y1:.1f}" width="{bw*0.84:.1f}" height="{hgt:.1f}" fill="{colors[k]}"/>')
            acc += v
        if i % step == 0:
            out.append(f'<text x="{xx+bw/2:.1f}" y="{height-8}" class="tick" text-anchor="middle">{esc(xfmt(ts))}</text>')
    if line50:
        y = pad_t + H / 2
        out.append(f'<line x1="{pad_l}" x2="{width-pad_r}" y1="{y:.1f}" y2="{y:.1f}" class="line50"/>')
    out.append("</svg>")
    lg = "".join(f'<span class="lg"><i style="background:{colors[k]}"></i>{esc(labels[k])}</span>' for k in range(len(keys)))
    return f'<div class="legend">{lg}</div><div class="cw">' + "".join(out) + "</div>"


def svg_donut(items, size=180):
    """items: [(label, pct, color)]"""
    import math
    r, cx, cy, sw = 62, size / 2, size / 2, 26
    out = [f'<svg viewBox="0 0 {size} {size}" class="donut" role="img">']
    a0 = -math.pi / 2
    for label, pct, col in items:
        if pct <= 0:
            continue
        a1 = a0 + 2 * math.pi * pct / 100
        x0, y0 = cx + r * math.cos(a0), cy + r * math.sin(a0)
        x1, y1 = cx + r * math.cos(a1), cy + r * math.sin(a1)
        large = 1 if (a1 - a0) > math.pi else 0
        out.append(f'<path d="M{x0:.1f},{y0:.1f} A{r},{r} 0 {large} 1 {x1:.1f},{y1:.1f}" fill="none" stroke="{col}" stroke-width="{sw}"><title>{esc(label)}: {fnum(pct,1)}%</title></path>')
        a0 = a1
    out.append(f'<line x1="{cx}" y1="{cy-r-sw/2-4}" x2="{cx}" y2="{cy+r+sw/2+4}" class="line50"/>')
    out.append("</svg>")
    return "".join(out)


# ---------------------------------------------------------------- data

def load(con):
    d = {}
    d["blocks"] = con.execute("SELECT height,timestamp,miner,difficulty,base_difficulty,multiplier,tx_count FROM blocks ORDER BY height").fetchall()
    d["snap"] = con.execute("SELECT * FROM snapshots ORDER BY ts DESC LIMIT 1").fetchone()
    d["snap_cols"] = [c[1] for c in con.execute("PRAGMA table_info(snapshots)")]
    d["snaps"] = con.execute("SELECT ts,price_usdt,holders,work_rate_24h,market_cap,mempool FROM snapshots ORDER BY ts").fetchall()
    d["p60"] = con.execute("SELECT ts,open,high,low,close,volume FROM prices WHERE period=60 ORDER BY ts").fetchall()
    d["p1440"] = con.execute("SELECT ts,open,high,low,close,volume FROM prices WHERE period=1440 ORDER BY ts").fetchall()
    d["activity"] = con.execute("SELECT day,blocks,transactions,transfers,new_addresses,fees_sats,gross_output_sats FROM daily_activity ORDER BY day").fetchall()
    d["aliases"] = dict(con.execute("SELECT address,alias FROM aliases").fetchall())
    hs = con.execute("SELECT MAX(ts) FROM holders_snapshots").fetchone()[0]
    d["holders_ts"] = hs
    d["holders"] = con.execute("SELECT rank,address,balance,net_flow_7d,net_flow_30d,supply_share,tx_count,last_seen_ts FROM holders_snapshots WHERE ts=? ORDER BY rank", (hs,)).fetchall() if hs else []
    d["meta"] = dict(con.execute("SELECT key,value FROM meta").fetchall())
    try:
        d["proof"] = con.execute("SELECT height,timestamp,miner,proof_version,proof_mult,p_late,p_profile,p_state,near_pin FROM blocks "
                                 "WHERE height>=? AND proof_version IS NOT NULL ORDER BY height", (guides.V4_PRICING,)).fetchall()
    except sqlite3.OperationalError:
        d["proof"] = []
    try:
        d["releases"] = guides.parse_releases(json.loads(d["meta"].get("releases_json") or "[]"))
    except (ValueError, TypeError):
        d["releases"] = []
    return d


def snap_val(d, key):
    if not d["snap"]:
        return None
    return d["snap"][d["snap_cols"].index(key)]


def epochs_from_snapshot(d):
    try:
        raw = json.loads(snap_val(d, "raw") or "{}")
        eps = raw.get("supply", {}).get("epochs") or []
        out = [(e["start_height"], e["end_height"], e["reward_sats"] / SATS) for e in eps]
        return out or EPOCHS_FALLBACK
    except Exception:
        return EPOCHS_FALLBACK


def reward_at(epochs, h):
    for s, e, r in epochs:
        if s <= h < e:
            return r
    return epochs[-1][2] / 2 if epochs else 0.0


def compute(d):
    blocks = d["blocks"]
    if not blocks:
        raise SystemExit("No blocks in the database — run collect.py first")
    epochs = epochs_from_snapshot(d)
    tip_h, tip_ts = blocks[-1][0], blocks[-1][1]
    now = int(time.time())
    ref = max(tip_ts, now - 3600)  # window end: now (never earlier than the tip)
    c = {"tip_h": tip_h, "tip_ts": tip_ts, "now": now, "ref": ref, "epochs": epochs, "genesis_ts": blocks[0][1]}

    def window(sec, end=None):
        t0 = (end or ref) - sec
        w = [b for b in blocks if b[1] > t0]
        work = sum(b[3] for b in w)
        n = len(w)
        bt = None
        if n >= 2:
            bt = (w[-1][1] - w[0][1]) / (n - 1)
        return {"blocks": n, "work": work, "rate": work / sec, "block_time": bt,
                "new_tsc": sum(reward_at(epochs, b[0]) for b in w),
                "miners": defaultdict(lambda: {"blocks": 0, "work": 0.0, "last": None}), "_list": w}

    for name, sec in (("1h", 3600), ("24h", DAY), ("7d", 7 * DAY), ("30d", 30 * DAY)):
        w = window(sec, tip_ts if name == "1h" else None)
        for b in w["_list"]:
            m = w["miners"][b[2]]
            m["blocks"] += 1
            m["work"] += b[3]
            m["last"] = b
        c[name] = w
    total_work = sum(b[3] for b in blocks)
    all_m = defaultdict(lambda: {"blocks": 0, "work": 0.0})
    for b in blocks:
        all_m[b[2]]["blocks"] += 1
        all_m[b[2]]["work"] += b[3]
    c["all"] = {"blocks": len(blocks), "work": total_work, "miners": all_m,
                "rate": total_work / max(1, tip_ts - blocks[0][1])}

    # hourly series: work per hour → rate; plus rolling 24h
    hourly = defaultdict(float)
    for b in blocks:
        hourly[b[1] // 3600 * 3600] += b[3]
    h0 = blocks[0][1] // 3600 * 3600
    h1 = ref // 3600 * 3600
    hours = list(range(h0, h1 + 1, 3600))
    rate_h = [(h, hourly.get(h, 0.0) / 3600) for h in hours]
    roll = []
    acc = 0.0
    q = []
    for h in hours:
        acc += hourly.get(h, 0.0)
        q.append(h)
        while q and q[0] <= h - DAY:
            acc -= hourly.get(q.pop(0), 0.0)
        roll.append((h, acc / DAY))
    c["rate_hourly"] = rate_h
    c["rate_24h_roll"] = roll
    full = [p for p in roll if p[0] >= h0 + DAY]
    c["ath_24h"] = max(full, key=lambda p: p[1]) if full else (None, None)

    # daily (UTC): blocks, work, avg difficulty, block time, new TSC, miner shares
    daily = defaultdict(lambda: {"blocks": 0, "work": 0.0, "diff": 0.0, "new_tsc": 0.0, "miners": defaultdict(int), "first": None, "last": None})
    for b in blocks:
        x = daily[b[1] // DAY * DAY]
        x["blocks"] += 1
        x["work"] += b[3]
        x["diff"] += b[4]
        x["new_tsc"] += reward_at(epochs, b[0])
        x["miners"][b[2]] += 1
        x["first"] = x["first"] or b[1]
        x["last"] = b[1]
    c["daily"] = dict(sorted(daily.items()))
    return c


def miner_name(d, addr):
    return d["aliases"].get(addr) or short_addr(addr)


# ---------------------------------------------------------------- HTML

CSS = """
:root{--acc:#731D30;--acc-deep:#501421;--acc-tint:rgba(115,29,48,.08);--bg:#F7F8F9;--paper:#EEF0F2;--line:#D8DCDF;--ink:#0F1115;--mute:#5B6168;--soft:#969BA1;--card:#FFFFFF;
--sans:"Inter",system-ui,-apple-system,"Segoe UI",sans-serif;--mono:"JetBrains Mono","SF Mono",Consolas,monospace}
*{box-sizing:border-box}body{margin:0;font-family:var(--sans);background:var(--bg);color:var(--ink);line-height:1.5;-webkit-font-smoothing:antialiased;font-feature-settings:"cv11","ss01"}
a{color:var(--acc);text-decoration-color:rgba(115,29,48,.35);text-underline-offset:3px}a:hover{color:var(--acc-deep)}
header{position:sticky;top:0;z-index:50;background:rgba(247,248,249,.85);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);border-bottom:1px solid var(--line)}
.top{max-width:1100px;margin:0 auto;padding:12px 16px;display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
.brand{font-weight:700;font-size:19px;letter-spacing:-.02em;text-decoration:none;color:var(--ink)}.brand span{color:var(--acc)}
main{max-width:1100px;margin:0 auto;padding:16px}
.hero{padding:34px 0 10px;margin-bottom:18px;border-bottom:1px solid var(--line)}
.hero h1 em{font-style:normal;color:var(--acc)}.hero h1{margin:0 0 10px;font-size:44px;line-height:1.08;font-weight:600;letter-spacing:-.035em;color:var(--ink);max-width:900px}
.hero p.lead{margin:0 0 16px;color:var(--mute);font-size:17px;max-width:760px}
.hero .snap{display:inline-block;border:1px solid var(--line);background:var(--card);border-radius:999px;padding:6px 14px;font:11px/1.4 var(--mono);text-transform:uppercase;letter-spacing:.12em;color:var(--soft);margin-bottom:16px}.hero .snap b{color:var(--acc);font-weight:500}
.hero .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));border-top:1px solid var(--line)}
.hero .cell{padding:18px 16px 18px 0;border-bottom:1px solid var(--line)}
.k,.hero .cell .k,.kpi .k,.ekpis .k{font:11px/1.4 var(--mono);text-transform:uppercase;letter-spacing:.12em;color:var(--soft)}
.hero .cell .v{font-size:34px;font-weight:600;letter-spacing:-.03em;margin:4px 0;font-variant-numeric:tabular-nums}.hero .cell .s{font-size:13px;color:var(--mute)}
.up{color:#2F7A55}.down{color:#B42318}.warn{color:#B26A00}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:22px 24px;margin-bottom:16px}
.card h2{margin:0 0 4px;font-size:21px;font-weight:600;letter-spacing:-.02em}.card h3{margin:18px 0 6px;font-size:16px;font-weight:600;letter-spacing:-.01em}.card p.sub{margin:0 0 14px;color:var(--mute);font-size:14.5px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:16px}.kpis.k3{grid-template-columns:repeat(3,1fr)}.kpi.locked{position:relative;background:#F1F2F4;border-style:dashed;color:var(--soft);cursor:not-allowed;user-select:none}.kpi.locked .v{color:#B9BDC3;letter-spacing:.12em}.kpi.locked .s{color:var(--soft)}.pro{display:inline-block;margin-left:6px;padding:1px 7px;border-radius:999px;background:var(--acc);color:#fff;font:600 9.5px var(--mono);letter-spacing:.1em;vertical-align:1px}@media(max-width:760px){.kpis.k3{grid-template-columns:1fr 1fr}}@media(max-width:480px){.kpis.k3{grid-template-columns:1fr}}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}.kpi .v{font-size:26px;font-weight:600;letter-spacing:-.03em;margin:4px 0 2px;font-variant-numeric:tabular-nums}.kpi .s{font-size:12.5px;color:var(--mute)}
.chart{width:100%;height:auto;display:block}.grid{stroke:#E6E8EB;stroke-width:1}.tick{font:10.5px var(--mono);fill:var(--soft)}.marker{stroke:#B26A00;stroke-width:1;stroke-dasharray:3 3}.mlabel{font:10.5px var(--mono);fill:#B26A00}.line50{stroke:#B42318;stroke-width:1.2;stroke-dasharray:5 4}
.cw{position:relative}.xh{stroke:var(--ink);stroke-width:1;opacity:.3}.xb{fill:var(--acc);opacity:.07}
.tip{position:absolute;display:none;pointer-events:none;z-index:5;background:#fff;border:1px solid var(--line);border-radius:10px;box-shadow:0 8px 24px rgba(15,17,21,.10);padding:8px 11px;font-size:12px;line-height:1.55;white-space:nowrap;color:var(--ink)}
.tip .th{font:11px var(--mono);text-transform:uppercase;letter-spacing:.08em;color:var(--soft);margin-bottom:3px}.tip i{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:6px}.tip b{font-weight:600;font-variant-numeric:tabular-nums}
.links{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:10px}
.lnk{display:flex;flex-direction:column;gap:3px;padding:13px 15px;border:1px solid var(--line);border-radius:12px;text-decoration:none;color:var(--ink);background:var(--card);transition:border-color .15s,background .15s}
.lnk:hover{border-color:rgba(115,29,48,.45);background:#FCFAFA}.lt{font-weight:600}.ext{color:var(--acc);font-weight:500}.ld{font-size:13px;color:var(--mute)}.lu{font:11.5px var(--mono);color:var(--acc)}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--mute);margin:4px 0 8px}.lg i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px}
table{width:100%;border-collapse:collapse;font-size:14px;font-variant-numeric:tabular-nums}th,td{padding:9px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}th{font:10.5px/1.4 var(--mono);text-transform:uppercase;letter-spacing:.1em;color:var(--soft);font-weight:400}td:first-child,th:first-child{text-align:left}
.mono,code{font-family:var(--mono);font-size:12.5px}
.tabs{display:inline-flex;gap:6px;margin-bottom:10px;flex-wrap:wrap}.tabs button{border:1px solid var(--line);background:var(--card);padding:5px 12px;border-radius:999px;font:11px var(--mono);text-transform:uppercase;letter-spacing:.1em;color:var(--mute);cursor:pointer}.tabs button:hover{border-color:rgba(115,29,48,.4)}.tabs button.on{background:var(--acc-tint);border-color:rgba(115,29,48,.35);color:var(--acc)}
.pane{display:none}.pane.on{display:block}
.two{display:grid;grid-template-columns:1fr 1fr;gap:16px}@media(max-width:800px){.two{grid-template-columns:1fr}.hero h1{font-size:32px}.hero .cell .v{font-size:27px}}
.donut{width:180px;height:180px}.donutwrap{display:flex;gap:20px;align-items:center;flex-wrap:wrap}
.note{font-size:13px;color:var(--mute)}.empty{padding:30px;text-align:center;color:var(--mute)}
footer{max-width:1100px;margin:28px auto 0;padding:22px 16px 44px;color:var(--soft);font:11px/1.9 var(--mono);text-transform:uppercase;letter-spacing:.08em;border-top:1px solid var(--line)}footer{display:flex;align-items:center;gap:18px;flex-wrap:wrap;font-size:13px}footer a{color:var(--mute)}footer a:hover{color:var(--acc)}footer .xl{display:inline-flex;align-items:center;justify-content:center;width:38px;height:38px;border:1px solid var(--line);border-radius:999px;background:#fff}footer .xl:hover{border-color:var(--acc)}footer .xl img{width:18px;height:18px;display:block;opacity:.85}footer .xl:hover img{opacity:1}
.badge{display:inline-block;padding:3px 10px;border-radius:999px;font:10.5px var(--mono);text-transform:uppercase;letter-spacing:.08em;background:var(--acc-tint);color:var(--acc)}
dl{display:grid;grid-template-columns:max-content 1fr;gap:8px 18px;font-size:14px}dt{color:var(--mute)}dd{margin:0}@media(max-width:600px){dl{grid-template-columns:1fr}}
"""

JS = """
(function(){const MON=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'],NS='http://www.w3.org/2000/svg',z=n=>String(n).padStart(2,'0');
function fd(ts,daily){const d=new Date(ts*1000);let s=z(d.getUTCDate())+' '+MON[d.getUTCMonth()]+' '+d.getUTCFullYear();return daily?s:s+', '+z(d.getUTCHours())+':'+z(d.getUTCMinutes())+' UTC'}
function el(t,a){const e=document.createElementNS(NS,t);for(const k in a)e.setAttribute(k,a[k]);return e}
function near(a,x){let lo=0,hi=a.length-1;while(hi-lo>1){const m=(lo+hi)>>1;if(a[m][0]<x)lo=m;else hi=m}return Math.abs(a[lo][0]-x)<=Math.abs(a[hi][0]-x)?a[lo]:a[hi]}
window.tscTip=function(svg){if(!svg||svg.__tip)return;svg.__tip=1;
 const T=JSON.parse(svg.dataset.tip),wrap=svg.parentNode,g=el('g',{'pointer-events':'none'});g.style.display='none';
 const vl=T.bw?el('rect',{class:'xb',y:T.b[0],height:T.b[1]-T.b[0],width:T.bw}):el('line',{class:'xh',y1:T.b[0],y2:T.b[1]});g.appendChild(vl);
 const dots=(T.s||[]).map(s=>{const c=el('circle',{r:3.5,fill:s[1],stroke:'#fff','stroke-width':1.5});g.appendChild(c);return c});
 svg.appendChild(g);const tip=document.createElement('div');tip.className='tip';wrap.appendChild(tip);
 const row=(n,c,v)=>'<div><i style="background:'+c+'"></i>'+n+': <b>'+v+'</b></div>';
 function hide(){g.style.display='none';tip.style.display='none'}
 function move(cx){const r=svg.getBoundingClientRect(),k=svg.viewBox.baseVal.width/r.width,x=(cx-r.left)*k;
  if(x<T.l-4||x>T.r+4)return hide();let hx=null,ts=null,rows='';
  if(T.bars){const b=near(T.bars,x);hx=b[0];ts=b[1];rows=b[2].map(q=>row(q[0],q[1],q[2])).join('');vl.setAttribute('x',hx-T.bw/2)}
  else{T.s.forEach((s,i)=>{const a=s[2];if(!a.length||x<a[0][0]-8||x>a[a.length-1][0]+8){dots[i].style.display='none';return}
   const p=near(a,x);if(hx===null){hx=p[0];ts=p[2]}dots[i].style.display='';dots[i].setAttribute('cx',p[0]);dots[i].setAttribute('cy',p[1]);rows+=row(s[0],s[1],p[3])});
   if(hx===null)return hide();vl.setAttribute('x1',hx);vl.setAttribute('x2',hx)}
  g.style.display='';tip.innerHTML='<div class="th">'+fd(ts,T.d)+'</div>'+rows;tip.style.display='block';
  const px=hx/k,w=tip.offsetWidth;let left=px+14;if(left+w>r.width)left=px-w-14;if(left<0)left=0;
  const wr=wrap.getBoundingClientRect();tip.style.left=(r.left-wr.left+left)+'px';tip.style.top=(r.top-wr.top+6)+'px'}
 svg.addEventListener('mousemove',e=>move(e.clientX));svg.addEventListener('mouseleave',hide);
 svg.addEventListener('touchstart',e=>move(e.touches[0].clientX),{passive:true});svg.addEventListener('touchmove',e=>move(e.touches[0].clientX),{passive:true});
};document.querySelectorAll('svg[data-tip]').forEach(window.tscTip);})();
document.querySelectorAll('.tabs').forEach(t=>{t.querySelectorAll('button').forEach(b=>b.addEventListener('click',()=>{
 t.querySelectorAll('button').forEach(x=>x.classList.remove('on'));b.classList.add('on');
 const g=t.dataset.group;document.querySelectorAll('.pane[data-group="'+g+'"]').forEach(p=>p.classList.toggle('on',p.dataset.key===b.dataset.key));}))});
"""

DOMAIN_FILE = os.path.join(ROOT, "data", "domain.txt")
DESC = "Independent TensorCash (TSC) dashboard: SafeTrade price and volume, issuance, network work rate, mining pools, top holders. Computed from the chain, refreshed hourly."


def site_domain():
    if os.environ.get("SITE_DOMAIN"):
        return os.environ["SITE_DOMAIN"].strip()
    try:
        return open(DOMAIN_FILE).read().strip() or None
    except OSError:
        return None


def brand(dom):
    if dom:
        parts = dom.split(".")
        return f'{esc(".".join(parts[:-1]))}<span>.{esc(parts[-1])}</span>'
    return 'tsc<span>.watch</span>'


NAV_GROUPS = [
    ("Market", [("market", "index.html", "Price & volume"), ("holders", "holders.html", "Holders")]),
    ("Mining", [("mining", "mining.html", "Network & blocks"), ("miners", "miner.html", "Miners & pools"),
                ("proof", "proof.html", "Proof efficiency"), ("calc", "calc.html", "Calculator")]),
    ("Resources", [("upgrades", "upgrades.html", "Upgrades & deadlines"), ("links", "links.html", "Official links"),
                   ("about", "about.html", "About the data"), ("contact", "contact.html", "Contact")]),
]


def nav_html(active):
    groups = []
    for i, (label, items) in enumerate(NAV_GROUPS):
        on = any(k == active for k, _, _ in items)
        links = "".join(f'<a href="{h}"{" class=on aria-current=page" if k == active else ""}>{esc(l)}</a>' for k, h, l in items)
        groups.append(f'<div class="grp{" on" if on else ""}"><button type="button" class="gbtn" aria-expanded="false" aria-controls="dd{i}">{label}<span class="car" aria-hidden="true"></span></button>'
                      f'<div class="drop" id="dd{i}"><span class="gl">{label}</span>{links}</div></div>')
    cta = f'<a class="cta{" on" if active == "start" else ""}" href="start.html">Start mining</a>'
    soon = "".join(f'<span class="soon" aria-disabled="true" title="Coming soon">{l}<em>soon</em></span>' for l in ("Wallet",))
    return (f'<button type="button" class="burger" aria-expanded="false" aria-controls="menu" aria-label="Menu"><span></span><span></span><span></span></button>'
            f'<nav id="menu" class="menu">{"".join(groups)}{soon}{cta}</nav>')


NAV_CSS = """
.top{position:relative}.brand{margin-right:auto}
.langs{display:flex;gap:2px;padding:2px;border:1px solid var(--line);border-radius:999px;background:#fff}
.lg{padding:5px 9px;border-radius:999px;color:var(--soft);text-decoration:none;font:11.5px var(--mono);letter-spacing:.04em;line-height:1}
.lg:hover{color:var(--acc);background:var(--acc-tint)}.lg.on{background:var(--acc);color:#fff}.lg:focus-visible{outline:2px solid var(--acc);outline-offset:2px}.menu{display:flex;align-items:center;gap:4px}
.grp{position:relative}.gbtn{display:flex;align-items:center;gap:6px;border:0;background:transparent;padding:7px 12px;border-radius:999px;color:var(--acc);font:500 14px var(--sans);cursor:pointer}
.gbtn:hover,.gbtn[aria-expanded=true]{background:var(--acc-tint)}.grp.on>.gbtn{color:var(--acc-deep);font-weight:600;background:var(--acc-tint)}
.gbtn:focus-visible,.cta:focus-visible,.drop a:focus-visible,.burger:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
.car{width:6px;height:6px;border-right:1.5px solid currentColor;border-bottom:1.5px solid currentColor;transform:rotate(45deg) translateY(-2px);transition:transform .15s}
.gbtn[aria-expanded=true] .car{transform:rotate(-135deg) translateY(-1px)}
.drop{display:none;position:absolute;top:calc(100% + 6px);left:0;min-width:210px;background:#fff;border:1px solid var(--line);border-radius:12px;box-shadow:0 12px 32px rgba(15,17,21,.12);padding:6px;z-index:60}
.gbtn[aria-expanded=true]+.drop{display:block}
.drop a{display:block;padding:9px 12px;border-radius:8px;text-decoration:none;color:var(--ink);font-size:14px}.drop a:hover{background:var(--acc-tint);color:var(--acc)}
.drop a.on{color:var(--acc);font-weight:600;background:var(--acc-tint)}.drop .gl{display:none}
.cta{margin-left:8px;padding:8px 16px;border-radius:999px;border:1px solid rgba(115,29,48,.35);background:var(--acc);color:#fff;text-decoration:none;font:11.5px var(--mono);text-transform:uppercase;letter-spacing:.1em}
.cta:hover{background:var(--acc-deep);color:#fff}.cta.on{background:var(--acc-deep)}
.burger{display:none;width:40px;height:40px;border:1px solid var(--line);border-radius:10px;background:#fff;cursor:pointer;flex-direction:column;justify-content:center;align-items:center;gap:4px}
.burger span{display:block;width:16px;height:1.5px;background:var(--ink);transition:transform .15s,opacity .15s}
.burger[aria-expanded=true] span:nth-child(1){transform:translateY(5.5px) rotate(45deg)}.burger[aria-expanded=true] span:nth-child(2){opacity:0}.burger[aria-expanded=true] span:nth-child(3){transform:translateY(-5.5px) rotate(-45deg)}
@media(max-width:760px){.burger{display:flex;order:3}.langs{order:2}.menu{display:none;position:absolute;top:100%;left:0;right:0;flex-direction:column;align-items:stretch;gap:0;background:var(--bg);border-bottom:1px solid var(--line);padding:6px 16px 16px;box-shadow:0 16px 24px rgba(15,17,21,.08)}
.menu.open{display:flex}.grp{border-bottom:1px solid var(--line);padding:8px 0}.gbtn{display:none}.drop{display:grid;grid-template-columns:1fr 1fr;position:static;box-shadow:none;border:0;padding:0;background:transparent;min-width:0}
.drop .gl{display:block;grid-column:1/-1;font:10.5px var(--mono);text-transform:uppercase;letter-spacing:.12em;color:var(--soft);padding:6px 12px 2px}
.cta{margin:14px 0 0;text-align:center;padding:12px}}
@media(prefers-reduced-motion:reduce){.car,.burger span{transition:none}}
"""

THEME_CSS = """
:root{--bg:#ECEFEB;--paper:#F3F5F3;--line:#D3D9D6;--ink:#17232A;--mute:#5C6C71;--soft:#7F8D90;--dark:#17262E;--dark-line:#33454E;--dark-mute:#9FB0B5;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,"JetBrains Mono",monospace}
body{font-feature-settings:normal}
header{background:var(--dark);backdrop-filter:none;-webkit-backdrop-filter:none;border-bottom:0}
.top{padding:14px 16px}.brand{color:#fff;font-weight:700;letter-spacing:-.01em}.brand span{color:#E6B7C1}
.gbtn{color:#D6DEE0;border-radius:3px;font-weight:500}.gbtn:hover,.gbtn[aria-expanded=true]{background:rgba(255,255,255,.08);color:#fff}
.grp.on>.gbtn{color:#fff;background:transparent;box-shadow:inset 0 0 0 1px #3A4C55;font-weight:600}
.drop{border-radius:3px;box-shadow:0 12px 32px rgba(15,17,21,.18)}.drop a{border-radius:2px}
.cta{border-radius:3px;border:0;font:750 13px var(--sans);text-transform:none;letter-spacing:0;padding:10px 16px}
.langs{border:1px solid #3A4C55;border-radius:3px;background:transparent;padding:0;gap:0}.lg{border-radius:0;color:#C9D3D6;padding:8px 10px;font-weight:600}
.lg:hover{background:rgba(255,255,255,.08);color:#fff}.lg.on{background:#fff;color:var(--dark)}
.burger{background:transparent;border-color:#3A4C55;border-radius:3px}.burger span{background:#fff}
.soon{display:inline-flex;align-items:center;gap:6px;padding:7px 10px;color:#6F8188;font:500 14px var(--sans);cursor:not-allowed;user-select:none}
.soon em{font:600 9.5px var(--mono);font-style:normal;text-transform:uppercase;letter-spacing:.1em;border:1px solid #3A4C55;color:#8FA0A6;padding:2px 6px;border-radius:2px}
.hero{border-bottom:0;padding:44px 0 18px}
.hero h1{font-weight:710;letter-spacing:-.05em;line-height:1.02;color:#151417}
.hero h1 em{font-family:Georgia,"Times New Roman",serif;font-weight:400;letter-spacing:-.05em;color:var(--acc)}
.hero .snap{border-radius:2px;background:transparent;border:0;padding:0;color:var(--acc);font-weight:700;letter-spacing:.2em}.hero .snap::before{content:"";display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--acc);margin-right:9px;vertical-align:1px}
.hero .grid{background:var(--card);border:1px solid var(--line);border-top:1px solid var(--line)}.hero .cell{padding:18px 20px;border-right:1px solid var(--line)}
.k,.hero .cell .k,.kpi .k,.ekpis .k{font:600 11px/1.4 var(--mono);letter-spacing:.06em;color:var(--mute)}
.hero .cell .v,.kpi .v{font-family:var(--mono);font-weight:700;letter-spacing:-.01em}
.hero .cell .v{font-size:28px}.kpi .v{font-size:24px}
.card,.kpi,.lnk,.mres .mcard,.health,.verdict,pre.code{border-radius:3px}
.card h2{font-weight:690;letter-spacing:-.02em}
.kpi.locked{background:var(--paper);border-color:#AEBBB8}.pro,.badge{border-radius:2px}
.tabs{gap:0;border:1px solid #AEBBB8;display:inline-flex}.tabs button{border:0;border-right:1px solid #AEBBB8;border-radius:0;background:var(--card);font:600 12px var(--sans);text-transform:none;letter-spacing:0;color:var(--ink);padding:8px 13px}.tabs button:last-child{border-right:0}
.tabs button.on{background:var(--acc-tint);color:var(--acc);border-color:#AEBBB8}
.look input,.look button,.seg button,.calc input,.calc select,.cform input,.cform select,.cform textarea,.cbtn,.pill,.labtag{border-radius:3px}
.tip{border-radius:3px}
th{font-weight:600;color:var(--mute);background:var(--paper)}
footer{border-top:1px solid var(--line)}footer .xl{border-color:#AEBBB8}
@media(max-width:760px){.menu{background:var(--dark);border-bottom:0}.grp{border-bottom:1px solid #33454E}.drop a{color:#E3E9EB}.drop a:hover,.drop a.on{background:rgba(255,255,255,.08);color:#fff}.drop .gl{color:#8FA0A6}.soon{padding:12px 12px;border-bottom:1px solid #33454E}}
.hero .cell:last-child{border-right:0}
main,.top,footer{max-width:1360px}
.strip{background:#0F1B21;color:#C9D3D6;font:500 11px var(--mono);letter-spacing:.08em;text-transform:uppercase;display:flex;justify-content:space-between;gap:12px;padding:7px max(16px,calc((100% - 1328px)/2))}
.strip span{display:flex;align-items:center;gap:8px}.strip b{color:#fff;font-weight:600}.strip .sq{width:6px;height:6px;background:var(--acc);display:inline-block}.strip .dot{width:7px;height:7px;border-radius:50%;background:#3FA37A;display:inline-block}
.eyebrow{font:700 11px var(--mono);letter-spacing:.2em;text-transform:uppercase;color:var(--acc);display:flex;align-items:center;gap:9px}.eyebrow::before{content:"";width:6px;height:6px;border-radius:50%;background:var(--acc)}
.eyebrow.sm{letter-spacing:.1em;margin-bottom:4px}.eyebrow.sm::before{display:none}
.hx{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:40px;align-items:end;padding:48px 0 32px}.hx h1{font-size:56px;margin:0 0 14px}.hx h1 em{font-size:.7em;letter-spacing:-.04em}.hx .lead{margin:0}
.hx-r{display:flex;gap:12px;justify-content:flex-end;flex-wrap:wrap;padding-bottom:6px}
.btn{display:inline-block;background:var(--acc);color:#fff;text-decoration:none;font-weight:750;font-size:14px;padding:14px 20px;border-radius:6px;border:1px solid var(--acc)}.btn:hover{background:var(--acc-deep);color:#fff}
.btn.ghost{background:#fff;color:var(--acc);border-color:#AEBBB8}.btn.ghost:hover{border-color:var(--acc);background:#fff;color:var(--acc)}
.live{background:#fff;border:1px solid var(--line);margin-bottom:16px}
.live-h{background:var(--dark);color:#fff;padding:13px 20px;display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}.live-h>span:first-child{font-size:13px;font-weight:690;letter-spacing:.08em;text-transform:uppercase}
.live-h .src{font:500 11px var(--mono);color:#D9A441;display:flex;align-items:center;gap:7px}.live-h .src i{width:7px;height:7px;border-radius:50%;background:#D9A441;display:inline-block}
.live-g{display:grid;grid-template-columns:repeat(4,minmax(0,1fr))}.live-g .cell{padding:18px 20px;border-right:1px solid var(--line)}.live-g .cell:last-child{border-right:0}
.live .v,.net .v,.side .v{font-family:var(--mono);font-weight:700;font-size:26px;letter-spacing:-.01em;margin:6px 0 4px;font-variant-numeric:tabular-nums}.live .s{font-size:13px;color:var(--mute)}
.net{background:var(--dark);color:#fff;display:grid;grid-template-columns:repeat(4,minmax(0,1fr));padding:22px 0;margin-bottom:16px}
.net .cell{padding:0 22px;border-left:1px solid var(--dark-line)}.net .cell:first-child{border-left:0}.net .k{color:var(--dark-mute)}.net .s{font-size:13px;color:var(--dark-mute)}.net .s.up{color:#6FCF9F}.net .s.down{color:#F08A97}
.net .v.big{font-size:32px}.net small{font-size:15px;color:var(--dark-mute);font-weight:500}
.mgrid{display:grid;grid-template-columns:2fr 1fr;gap:16px;margin-bottom:16px}.mgrid>.card{margin:0}.side{display:flex;flex-direction:column;gap:16px}.side .kpi{flex:1}
.prod{padding:0}.prod .ch{display:flex;justify-content:space-between;align-items:flex-end;padding:20px 24px 16px;gap:12px}.prod h2{margin:0}.more{font:700 13px var(--mono)}
.pr{display:grid;grid-template-columns:2fr 3fr 1fr 1.3fr;gap:12px;align-items:center;padding:13px 24px;border-top:1px solid #E7EBE8;font-size:14px}
.pr.ph{background:var(--paper);border-top:1px solid var(--line);border-bottom:1px solid var(--line);font:600 11px var(--mono);letter-spacing:.06em;text-transform:uppercase;color:var(--mute);padding:11px 24px}
.pr .sh{display:flex;align-items:center;gap:10px;font-family:var(--mono)}.pr .sh i{display:inline-block;height:8px;max-width:70%}.pr .addr{color:#2E617B}
.new{font:700 10.5px var(--sans);text-transform:uppercase;letter-spacing:.06em;color:var(--acc);background:var(--acc-tint);padding:2px 7px;margin-left:6px}
@media(max-width:900px){.hx{grid-template-columns:1fr;gap:20px;padding:32px 0 24px}.hx h1{font-size:38px}.hx-r{justify-content:flex-start}.hx-r .btn{flex:1;text-align:center}
.live-g{grid-template-columns:1fr 1fr}.live-g .cell:nth-child(2){border-right:0}.live-g .cell:nth-child(-n+2){border-bottom:1px solid var(--line)}
.net{grid-template-columns:1fr 1fr;row-gap:18px}.net .cell:nth-child(3){border-left:0}.mgrid{grid-template-columns:1fr}
.pr{grid-template-columns:1.6fr 1.4fr .6fr;padding:12px 16px}.pr>span:nth-child(4){display:none}.prod .ch{padding:16px}.strip>span:first-child{display:none}}
@media(max-width:480px){.live .v,.side .v{font-size:20px}.net .v,.net .v.big{font-size:21px}.hx-r{flex-direction:column}.hx-r .btn{width:100%}.net .cell{padding:0 14px}}
.hero:not(.hx){padding:44px 0 22px}.hero:not(.hx) h1{font-size:48px;margin-bottom:14px}
.hero:not(.hx) .grid{background:var(--dark);border:0;color:#fff;margin-top:6px}
.hero:not(.hx) .cell{border-right:1px solid var(--dark-line);border-bottom:0;padding:22px}.hero:not(.hx) .cell:last-child{border-right:0}
.hero:not(.hx) .cell .k,.hero:not(.hx) .cell .s{color:var(--dark-mute)}.hero:not(.hx) .cell .v{color:#fff;font-size:28px}
.hero:not(.hx) .cell a{color:#E6B7C1}.hero:not(.hx) .up{color:#6FCF9F}.hero:not(.hx) .down,.hero:not(.hx) .v.down{color:#F08A97}.hero:not(.hx) .warn,.hero:not(.hx) .v.warn{color:#E0B45A}
.look{gap:0;margin:10px 0 20px}.look input{border-radius:3px 0 0 3px;border-color:#AEBBB8;padding:14px 16px;font-size:15px}.look button{border-radius:0 3px 3px 0;background:var(--acc);border-color:var(--acc);color:#fff;font:750 14px var(--sans);text-transform:none;letter-spacing:0;padding:14px 22px}.look button:hover{background:var(--acc-deep)}
.card table th{background:var(--paper)}.card>table,.card .tw>table{margin-top:8px}
@media(max-width:800px){.hero .cell{border-right:0}.hero:not(.hx) .cell{border-bottom:1px solid var(--dark-line)}.hero:not(.hx) .cell:last-child{border-bottom:0}.hero:not(.hx) h1{font-size:34px}}
"""

NAV_JS = """
(function(){const btns=[...document.querySelectorAll('.gbtn')],menu=document.getElementById('menu'),bur=document.querySelector('.burger');
const close=ex=>btns.forEach(b=>{if(b!==ex)b.setAttribute('aria-expanded','false')});
btns.forEach(b=>b.addEventListener('click',e=>{e.stopPropagation();const o=b.getAttribute('aria-expanded')==='true';close();b.setAttribute('aria-expanded',o?'false':'true')}));
document.addEventListener('click',e=>{if(!e.target.closest('.grp'))close()});
document.addEventListener('keydown',e=>{if(e.key==='Escape'){close();if(bur){bur.setAttribute('aria-expanded','false');menu.classList.remove('open')}}});
if(bur)bur.addEventListener('click',()=>{const o=menu.classList.toggle('open');bur.setAttribute('aria-expanded',o?'true':'false')});})();
"""


def page(title, active, body, gen_ts, tip_h, tip_ts, fn="index.html"):
    dom = site_domain()
    name = dom or "tsc.watch"
    og = (f'<meta property="og:title" content="{esc(title)} · {esc(name)}"><meta property="og:description" content="{esc(DESC)}">'
          f'<meta name="description" content="{esc(DESC)}"><meta name="twitter:card" content="summary">')
    href_of = {k: h for _, items in NAV_GROUPS for k, h, _ in items}
    href_of["start"] = "start.html"
    if dom:
        og += f'<link rel="canonical" href="https://{esc(dom)}/{"" if active == "market" else href_of.get(active, active + ".html")}">'
    og += i18n.hreflang(fn, dom)
    nav = nav_html(active) + i18n.switcher(fn)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · {esc(name)}</title><link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet"><meta name="theme-color" content="#17262E">{og}<style>{CSS}{NAV_CSS}{miners.CSS}{guides.CSS}{THEME_CSS}</style></head><body>
<div class="strip"><span><i class="sq"></i>Independent TensorCash dashboard</span><span><i class="dot"></i>Chain data {fdt(tip_ts, "%d %b, %H:%M UTC")} · <b>#{fnum(tip_h)}</b></span></div>
<header><div class="top"><a class="brand" href="index.html">{brand(dom)}</a>{nav}</div></header>
<main>{body}</main>
<footer><span>© 2026 tsc.watch</span><a class="xl" href="https://x.com/TSCwatch" target="_blank" rel="noopener noreferrer" aria-label="tsc.watch on X"><img src="https://cdn.jsdelivr.net/npm/simple-icons@13/icons/x.svg" alt="X" width="22" height="22"></a><a href="contact.html">Contact</a></footer>
<script>{JS}{NAV_JS}{miners.CALC_JS}{miners.MINER_JS}{miners.EPOCH_JS}{guides.START_JS}{guides.CONTACT_JS}</script></body></html>"""


def tabs_block(group, panes, default=0):
    btns = "".join(f'<button data-key="{k}" class="{"on" if i == default else ""}">{esc(l)}</button>' for i, (k, l, _) in enumerate(panes))
    ps = "".join(f'<div class="pane {"on" if i == default else ""}" data-group="{group}" data-key="{k}">{h}</div>' for i, (k, l, h) in enumerate(panes))
    return f'<div class="tabs" data-group="{group}">{btns}</div>{ps}'


def cls_pct(x):
    return "up" if (x or 0) > 0 else ("down" if (x or 0) < 0 else "")


# ---------------------------------------------------------------- page: Market

def build_market(d, c):
    p60, p1440 = d["p60"], d["p1440"]
    price = snap_val(d, "price_usdt") or (p60[-1][4] if p60 else None)
    ch24 = None
    if len(p60) >= 25 and p60[-25][4]:
        ch24 = (p60[-1][4] / p60[-25][4] - 1) * 100
    if ch24 is None:
        ch24 = snap_val(d, "change_24h")
    vol_usd = snap_val(d, "volume_24h_usdt")
    vol_tsc = snap_val(d, "volume_24h_tsc")
    if vol_tsc is None and len(p60) >= 24:
        vol_tsc = sum(r[5] for r in p60[-24:])
        vol_usd = sum(r[5] * r[4] for r in p60[-24:])
    circ = snap_val(d, "circ_supply")
    total = snap_val(d, "total_supply")
    mc = snap_val(d, "market_cap") or (price * circ if price and circ else None)
    fdv = snap_val(d, "fdv") or (price * total if price and total else None)
    holders = snap_val(d, "holders")
    new_tsc_24h = c["24h"]["new_tsc"]
    emis_usd = new_tsc_24h * price if price else None
    emis_ratio = (emis_usd / vol_usd * 100) if emis_usd and vol_usd else None
    price_7d = (p1440[-1][4] / p1440[-8][4] - 1) * 100 if len(p1440) >= 8 else None
    price_30d = (p1440[-1][4] / p1440[-31][4] - 1) * 100 if len(p1440) >= 31 else None
    ath = max(p1440, key=lambda r: r[2]) if p1440 else None

    blocks = d["blocks"]
    ref = c["ref"]
    rate24 = c["24h"]["rate"]
    prev_work = sum(b[3] for b in blocks if ref - 2 * DAY < b[1] <= ref - DAY)
    rate_ch = (rate24 / (prev_work / DAY) - 1) * 100 if prev_work else None
    epochs = c["epochs"]
    cur_ep = next((e for e in epochs if e[0] <= c["tip_h"] < e[1]), epochs[-1])

    hero = f"""
<section class="hero hx"><div class="hx-l">
<h1>The TensorCash market,<br><em>read from the blocks.</em></h1>
<p class="lead">Price and volume from SafeTrade, issuance, network work rate, mining pools and holders — one independent view, no account needed.</p></div>
<div class="hx-r"><a class="btn" href="calc.html">Open the miner calculator ↗</a><a class="btn ghost" href="upgrades.html">Upgrades &amp; deadlines</a></div></section>
<section class="live"><div class="live-h"><span>Live market</span><span class="src"><i></i>SafeTrade · TSC/USDT · {fdt(snap_val(d,'ts'),'%d %b, %H:%M UTC')}</span></div>
<div class="live-g">
<div class="cell"><div class="k">TSC price</div><div class="v">{fusd(price)}</div><div class="s"><span class="{cls_pct(ch24)}">{fpct(ch24,2)}</span> · 24h · 7d: <span class="{cls_pct(price_7d)}">{fpct(price_7d,1)}</span> · 30d: <span class="{cls_pct(price_30d)}">{fpct(price_30d,0)}</span></div></div>
<div class="cell"><div class="k">Market cap (issued)</div><div class="v">{fusd(mc,0)}</div><div class="s">{fshort(circ)} TSC issued · FDV {fusd(fdv,0)}</div></div>
<div class="cell"><div class="k">24h volume</div><div class="v">{fusd(vol_usd,0)}</div><div class="s">{fnum(vol_tsc,0)} TSC traded</div></div>
<div class="cell"><div class="k">Daily issuance vs. volume</div><div class="v">{fpct(emis_ratio,0,False)}</div><div class="s">{fnum(new_tsc_24h,0)} TSC mined in 24h ≈ {fusd(emis_usd,0)}</div></div>
</div></section>
<section class="net">
<div class="cell"><div class="k">Network work rate · 24h</div><div class="v big">{frate(rate24)}</div><div class="s {'up' if (rate_ch or 0) >= 0 else 'down'}">{'↗' if (rate_ch or 0) >= 0 else '↘'} {fpct(rate_ch,1)} vs. previous 24h</div></div>
<div class="cell"><div class="k">Effective difficulty</div><div class="v">{fshort(blocks[-1][3],2)}</div><div class="s">block #{fnum(c['tip_h'])} · ×{fnum(blocks[-1][5],2)} PoI multiplier</div></div>
<div class="cell"><div class="k">Block reward</div><div class="v">{fnum(cur_ep[2],4)} <small>TSC</small></div><div class="s">next cut at block #{fnum(cur_ep[1])}</div></div>
<div class="cell"><div class="k">Avg. block time · 24h</div><div class="v">{fdur(c['24h']['block_time'])}</div><div class="s">{fnum(c['24h']['blocks'])} blocks in 24h</div></div>
</section>"""

    side = f"""<div class="side">
<div class="kpi"><div class="k">Funded addresses</div><div class="v">{fnum(holders)}</div><div class="s">addresses with a balance</div></div>
<div class="kpi"><div class="k">Top 10 / top 100</div><div class="v">{fpct(snap_val(d,'top10_pct'),1,False)} / {fpct(snap_val(d,'top100_pct'),1,False)}</div><div class="s">counted per address — understates concentration: one early miner spread its coins over thousands of addresses</div></div>
<div class="kpi locked" aria-disabled="true"><div class="k">Free float <span class="pro">PRO</span></div><div class="v">•••••</div><div class="s">Supply that actually trades. For subscribers — coming soon.</div></div>
</div>"""

    last = blocks[-100:]
    first_seen = {}
    for b in blocks:
        first_seen.setdefault(b[2], b[1])
    cnt = defaultdict(int)
    for b in last:
        cnt[b[2]] += 1
    prod_rows = []
    for i, (a, n) in enumerate(sorted(cnt.items(), key=lambda kv: -kv[1])[:6]):
        sh = n / len(last) * 100
        fresh = first_seen.get(a, 0) >= c["now"] - 7 * DAY
        name = esc(d["aliases"][a]) if a in d["aliases"] else f'<span class="mono addr">{esc(short_addr(a))}</span>'
        tag = ' <span class="new">new</span>' if fresh else ''
        colr = "var(--acc)" if fresh else ("#17262E" if i == 0 else "#456E85")
        since = f'<span class="{"warn" if fresh else ""}">{fdt(first_seen.get(a), "%d %b %Y")}</span>'
        prod_rows.append(f'<div class="pr"><span>{name}{tag}</span><span class="sh"><i style="width:{sh:.0f}%;background:{colr}"></i>{fnum(sh,0)}%</span><span class="mono">{n}</span><span>{since}</span></div>')
    producers = f"""<section class="card prod"><div class="ch"><div><div class="eyebrow sm">Mining</div><h2>Who produced the last {len(last)} blocks</h2></div><a class="more" href="miner.html">See all pools</a></div>
<div class="pr ph"><span>Producer</span><span>Share</span><span>Blocks</span><span>First block</span></div>{''.join(prod_rows)}</section>"""

    kpis = f"""<div class="kpis">
<div class="kpi"><div class="k">All-time high (daily high)</div><div class="v">{fusd(ath[2]) if ath else '—'}</div><div class="s">{fdt(ath[0],'%d %b %Y') if ath else ''} · from ATH: {fpct((price/ath[2]-1)*100,0) if ath and price else '—'}</div></div>
{costs.market_tile(sys.modules[__name__], c['costs'], price)}
</div>"""

    def line_pts(rows, key=4):
        return [(r[0], r[key]) for r in rows]
    panes = []
    if p60:
        last24 = [r for r in p60 if r[0] >= p60[-1][0] - DAY]
        last7 = [r for r in p60 if r[0] >= p60[-1][0] - 7 * DAY]
        panes.append(("24h", "24h", svg_line([{"name": "price, 1h candles", "points": line_pts(last24)}], yfmt=lambda v: fusd(v, 2), xfmt=lambda x: fdt(x, "%H:%M"), y0=False, width=860)))
        panes.append(("7d", "7 days", svg_line([{"name": "price, 1h candles", "points": line_pts(last7)}], yfmt=lambda v: fusd(v, 2), xfmt=lambda x: fdt(x, "%d %b"), y0=False, width=860)))
    if p1440:
        panes.append(("30d", "30 days", svg_line([{"name": "daily close", "points": line_pts(p1440[-30:])}], yfmt=lambda v: fusd(v, 2), xfmt=fday, y0=False, width=860)))
        panes.append(("all", "since listing", svg_line([{"name": "daily close", "points": line_pts(p1440)}], yfmt=lambda v: fusd(v, 2), xfmt=fday, y0=True, width=860)))
    price_card = f"""<div class="mgrid"><section class="card"><div class="eyebrow sm">Price &amp; volume</div><h2>TSC/USDT on SafeTrade</h2><p class="sub">SafeTrade OHLC candles. Hourly for 24h/7d, daily for longer ranges (UTC days). Times in UTC.</p>{tabs_block('price', panes, 2 if len(panes) > 2 else 0)}</section>{side}</div>"""

    vol_pts = [(r[0], r[5] * r[4]) for r in p1440]
    emis_pts = []
    for day, x in c["daily"].items():
        close = next((r[4] for r in p1440 if r[0] == day), None)
        if close and day >= (p1440[0][0] if p1440 else 0):
            emis_pts.append((day, x["new_tsc"] * close))
    vol_card = f"""<section class="card"><h2>Daily volume vs. issuance</h2><p class="sub">Maroon: daily traded volume in USD (TSC × daily close). Amber: USD value of the TSC mined that day. When issuance approaches volume, miner supply alone can dominate the order book.</p>
{svg_line([{"name": "daily volume, USD", "points": vol_pts, "color": PALETTE[0]}, {"name": "daily issuance, USD", "points": emis_pts, "color": PALETTE[1], "area": False, "width": 2}], yfmt=lambda v: fusd(v, 0))}
</section>"""

    act = d["activity"]
    act_card = ""
    if act:
        tx = svg_bars([(r[0], r[2]) for r in act], width=560, yfmt=lambda v: fshort(v, 0), color=PALETTE[4])
        na = svg_bars([(r[0], r[4]) for r in act], width=560, yfmt=lambda v: fshort(v, 0), color=PALETTE[5])
        act_card = f"""<section class="card"><h2>Network activity (30 days)</h2><p class="sub">From the explorer: transactions and new addresses per UTC day. With exchange volume this thin, a high count of new addresses is mostly pool payouts, not new buyers.</p>
<div class="two"><div><b>Transactions / day</b>{tx}</div><div><b>New addresses / day</b>{na}</div></div></section>"""

    hist_card = ""
    snaps = d["snaps"]
    if len(snaps) >= 3:
        hist_card = f"""<section class="card"><h2>Trend: funded addresses and market cap</h2><p class="sub">Based on tsc.watch snapshots, one per data refresh. The chart grows longer every day.</p>
<div class="two"><div>{svg_line([{"name": "funded addresses", "points": [(s[0], s[2]) for s in snaps if s[2]]}], width=560, y0=False, xfmt=lambda x: fdt(x, "%d %b %H:%M"))}</div>
<div>{svg_line([{"name": "market cap, USD", "points": [(s[0], s[4]) for s in snaps if s[4]], "color": PALETTE[2]}], width=560, yfmt=lambda v: fusd(v, 0), xfmt=lambda x: fdt(x, "%d %b %H:%M"))}</div></div></section>"""

    epochs = c["epochs"]
    cur = next((e for e in epochs if e[0] <= c["tip_h"] < e[1]), epochs[-1])
    remaining = cur[1] - c["tip_h"]
    bt = c["7d"]["block_time"] or 600
    eta = c["tip_ts"] + remaining * bt
    rows = "".join(f"<tr><td>{i+1}</td><td>{fnum(s)}–{fnum(e-1)}</td><td>{fnum(r,4).rstrip('0').rstrip('.')} TSC</td><td>{'<span class=badge>current</span>' if s == cur[0] else ('completed' if e <= c['tip_h'] else 'upcoming')}</td></tr>"
                   for i, (s, e, r) in enumerate(epochs))
    supply_card = f"""<section class="card"><h2>Monetary policy</h2><p class="sub">The block reward drops 40% each epoch while epochs get longer. Max supply {fshort(total)} TSC.</p>
<div class="kpis">
<div class="kpi"><div class="k">Block reward</div><div class="v">{fnum(cur[2],4)} TSC</div><div class="s">next: {fnum(cur[2]*0.6,4)} TSC from block #{fnum(cur[1])}</div></div>
<div class="kpi"><div class="k">Until the next reward cut</div><div class="v">{fnum(remaining)} blocks</div><div class="s">≈ {fdt(eta,'%d %b %Y')} at {fdur(bt)}/block (7-day avg)</div></div>
<div class="kpi"><div class="k">New TSC per day</div><div class="v">{fnum(new_tsc_24h,0)}</div><div class="s">{fnum(c['24h']['blocks'])} blocks in 24h · 7-day avg: {fnum(c['7d']['new_tsc']/7,0)}/day</div></div>
<div class="kpi"><div class="k">Issued so far</div><div class="v">{fpct(circ/total*100 if circ and total else None,2,False)}</div><div class="s">{fshort(circ)} of {fshort(total)} TSC</div></div>
</div>
<table><thead><tr><th>Epoch</th><th>Blocks</th><th>Reward</th><th>Status</th></tr></thead><tbody>{rows}</tbody></table></section>"""

    return hero + price_card + producers + kpis + vol_card + supply_card + act_card + hist_card


# ---------------------------------------------------------------- page: Mining

def build_mining(d, c):
    w24, w7, w1, wall = c["24h"], c["7d"], c["1h"], c["all"]
    price = snap_val(d, "price_usdt") or (d["p60"][-1][4] if d["p60"] else None)
    rate24 = w24["rate"]
    expl24 = snap_val(d, "work_rate_24h")
    rew_1k = (w24["new_tsc"] * 1000 / rate24) if rate24 else None
    rew_1k_usd = rew_1k * price if rew_1k and price else None
    shares7 = sorted(((a, m["blocks"] / w7["blocks"] * 100) for a, m in w7["miners"].items()), key=lambda x: -x[1]) if w7["blocks"] else []
    acc, need, names = 0.0, 0, []
    for a, s in shares7:
        acc += s
        need += 1
        names.append(f"{miner_name(d, a)} {fnum(s,0)}%")
        if acc > 50:
            break
    top1_24 = max(((m["blocks"] / w24["blocks"] * 100, a) for a, m in w24["miners"].items()), default=(0, None)) if w24["blocks"] else (0, None)
    diff_now = d["blocks"][-1][4]
    ath_ts, ath_v = c["ath_24h"]
    n_active_24 = len(w24["miners"])

    hero = f"""
<section class="hero"><h1>Network <em>work rate</em> and who finds the <em>blocks</em>.</h1>
<p class="lead">How much proof-of-inference work secures the network, who finds the blocks and what mining earns. Everything computed from the chain (difficulty × blocks), refreshed hourly.</p>
<div class="snap">On-chain snapshot: <b>block #{fnum(c['tip_h'])}</b> · {fdt(c['tip_ts'])} · 24h window ends {fdt(c['ref'])}</div>
<div class="grid">
<div class="cell"><div class="k">Network work rate, 24h</div><div class="v">{frate(rate24)}</div><div class="s">last hour to tip: {frate(w1['rate'])} · explorer: {frate(expl24)} · highest 24h avg: {frate(ath_v)} ({fdt(ath_ts,'%d %b') if ath_ts else '—'})</div></div>
<div class="cell"><div class="k">Reward per 1 K proof/s a day</div><div class="v">{fnum(rew_1k,2) if rew_1k else '—'} TSC</div><div class="s">≈ {fusd(rew_1k_usd,2)} at {fusd(price)} · before pool fees, electricity and luck · last 24 hours</div></div>
<div class="cell"><div class="k">New TSC per day</div><div class="v">{fshort(w24['new_tsc'],2)}</div><div class="s">{fnum(w24['blocks'])} blocks × {fnum(reward_at(c['epochs'], c['tip_h']),4)} TSC · SafeTrade traded {fshort(snap_val(d,'volume_24h_tsc'),1)} TSC in 24h</div></div>
<div class="cell"><div class="k">Pools needed for a majority</div><div class="v {'down' if need == 1 else ('warn' if need == 2 else '')}">{need}</div><div class="s">{' + '.join(names)} of blocks, 7 days · share of blocks, not measured hashrate</div></div>
</div></section>"""

    kpis = f"""<div class="kpis">
<div class="kpi"><div class="k">Average block time, 24h</div><div class="v">{fdur(w24['block_time'])}</div><div class="s">protocol target ~10 min · 7d: {fdur(w7['block_time'])}</div></div>
<div class="kpi"><div class="k">Blocks, 24h</div><div class="v">{fnum(w24['blocks'])}</div><div class="s">7d: {fnum(w7['blocks'])} · since genesis: {fnum(wall['blocks'])}</div></div>
<div class="kpi"><div class="k">Difficulty now</div><div class="v">{fshort(diff_now,2)}</div><div class="s">base (bits) · PoI multiplier of the latest block ×{fnum(d['blocks'][-1][5],3)}</div></div>
<div class="kpi"><div class="k">Active miners, 24h</div><div class="v">{fnum(n_active_24)}</div><div class="s">payout addresses · largest: {fnum(top1_24[0],1)}% of blocks</div></div>
</div>"""

    roll, rh = c["rate_24h_roll"], c["rate_hourly"]
    markers = [(ath_ts, "highest 24h avg")] if ath_ts else []
    panes = [
        ("all", "since genesis", svg_line([{"name": "24-hour average", "points": roll, "width": 2, "scale": True}, {"name": "hourly estimate (noisy, clipped to scale)", "points": rh, "color": "#C9A6AF", "width": 0.8, "area": False, "opacity": .7}], yfmt=lambda v: fshort(v, 0), markers=markers, tipfmt=frate)),
        ("30d", "30 days", svg_line([{"name": "24-hour average", "points": [p for p in roll if p[0] >= roll[-1][0] - 30 * DAY], "width": 2}, {"name": "hourly estimate", "points": [p for p in rh if p[0] >= rh[-1][0] - 30 * DAY], "color": "#C9A6AF", "width": 0.8, "area": False, "opacity": .7}], yfmt=lambda v: fshort(v, 0), tipfmt=frate)),
        ("7d", "7 days", svg_line([{"name": "24-hour average", "points": [p for p in roll if p[0] >= roll[-1][0] - 7 * DAY], "width": 2}, {"name": "hourly estimate", "points": [p for p in rh if p[0] >= rh[-1][0] - 7 * DAY], "color": "#C9A6AF", "width": 1, "area": False}], yfmt=lambda v: fshort(v, 0), xfmt=lambda x: fdt(x, "%d %b"), tipfmt=frate)),
    ]
    rate_card = f"""<section class="card"><h2>Network work rate</h2><p class="sub">Combined work of all miners in proof/s (sum of effective block difficulty ÷ time). The hourly estimate is inherently noisy — at ~6 blocks per hour, luck dominates; the 24-hour average is the reliable line.</p>{tabs_block('rate', panes, 0)}</section>"""

    tops = sorted(w7["miners"].items(), key=lambda kv: -kv[1]["blocks"])
    top_addrs = [a for a, _ in tops[:5]]
    cols = PALETTE[:5] + ["#D8DCDF"]
    donut_items = [(miner_name(d, a), m["blocks"] / w7["blocks"] * 100, cols[i]) for i, (a, m) in enumerate(tops[:5])]
    donut_items.append(("Solo & other miners", 100 - sum(x[1] for x in donut_items), cols[5]))
    dl = "".join(f'<tr><td><i style="display:inline-block;width:10px;height:10px;border-radius:2px;background:{col};margin-right:6px"></i>{esc(n)}</td><td>{fnum(s,1)}%</td></tr>' for n, s, col in donut_items)

    def share(win, a):
        return win["miners"][a]["blocks"] / win["blocks"] * 100 if win["blocks"] and a in win["miners"] else 0.0
    rows = []
    for a, m in sorted(w24["miners"].items(), key=lambda kv: -kv[1]["blocks"]):
        rows.append(f"""<tr><td>{esc(miner_name(d,a))}{' <span class="badge">pool</span>' if a in d['aliases'] else ''}<div class="mono note">{esc(a)}</div></td>
<td>{fnum(m['blocks'])}</td><td>{fnum(share(w24,a),1)}%</td><td>{fnum(share(w7,a),1)}%</td><td>{fnum(wall['miners'][a]['blocks']/wall['blocks']*100,1) if a in wall['miners'] else '0.0'}%</td>
<td>{frate(m['work']/DAY)}</td><td>{fdt(m['last'][1],'%d %b %H:%M')}</td></tr>""")
    idle = [a for a in w7["miners"] if a not in w24["miners"]]
    idle_rows = "".join(f"""<tr><td>{esc(miner_name(d,a))}<div class="mono note">{esc(a)}</div></td><td>0</td><td>0.0%</td><td>{fnum(share(w7,a),1)}%</td><td>{fnum(wall['miners'][a]['blocks']/wall['blocks']*100,1)}%</td><td>—</td><td>{fdt(w7['miners'][a]['last'][1],'%d %b %H:%M')}</td></tr>""" for a in sorted(idle, key=lambda a: -w7["miners"][a]["blocks"])[:10])
    who_card = f"""<section class="card"><h2>Who finds the blocks?</h2><p class="sub">Every block records the payout address that produced it. Over many blocks, share of blocks ≈ share of work. Red line = 50%: one pool above it could in principle rewrite recent blocks; two pools together only by coordinating.</p>
<div class="donutwrap">{svg_donut(donut_items)}<div><b>Last 7 days, {fnum(w7['blocks'])} blocks</b><table>{dl}</table></div></div>
<table style="margin-top:14px"><thead><tr><th>Miner</th><th>Blocks, 24h</th><th>Share, 24h</th><th>Share, 7 days</th><th>Since genesis</th><th>Work rate, 24h</th><th>Latest block</th></tr></thead><tbody>{''.join(rows)}{idle_rows}</tbody></table>
<p class="note">Work rate per miner = sum of effective difficulty of its blocks in 24h ÷ 86,400 s. Over one day, ±3–5 points of share is ordinary luck. Pool aliases come from the explorer; unlabelled addresses are probably solo miners or unlabelled pools.</p></section>"""

    days = list(c["daily"].items())[-45:]
    keys = top_addrs + ["_other"]
    labels = [miner_name(d, a) for a in top_addrs] + ["Solo & other miners"]
    sd = []
    for day, x in days:
        tot = x["blocks"] or 1
        sh = {a: x["miners"].get(a, 0) / tot * 100 for a in top_addrs}
        sh["_other"] = 100 - sum(sh.values())
        sd.append((day, sh))
    stack_card = f"""<section class="card"><h2>Share of blocks per UTC day</h2><p class="sub">Last {len(days)} days, top 5 addresses of the past 7 days. Today is a partial day.</p>{svg_stacked(sd, keys, labels, cols)}</section>"""

    dd = [(day, x["diff"] / x["blocks"]) for day, x in c["daily"].items() if x["blocks"]]
    bt = [(day, (x["last"] - x["first"]) / (x["blocks"] - 1)) for day, x in c["daily"].items() if x["blocks"] > 2]
    diff_card = f"""<section class="card"><div class="two"><div><h2>Base difficulty</h2><p class="sub">Daily average from the bits field.</p>{svg_line([{"name": "difficulty (daily avg)", "points": dd, "color": PALETTE[3]}], width=560, yfmt=lambda v: fshort(v, 1), tipfmt=lambda v: fnum(v, 0))}</div>
<div><h2>Block time</h2><p class="sub">Average interval between blocks per UTC day; target ~600 s.</p>{svg_line([{"name": "seconds per block", "points": bt, "color": PALETTE[2]}, {"name": "600 s target", "points": [(bt[0][0], 600), (bt[-1][0], 600)] if bt else [], "color": "#999", "dash": True, "area": False}], width=560, yfmt=lambda v: fnum(v, 0))}</div></div></section>"""

    earn, earn_usd = [], []
    p1440 = {r[0]: r[4] for r in d["p1440"]}
    for day, x in list(c["daily"].items())[1:]:
        r = x["work"] / DAY
        if r > 0 and x["blocks"] > 0:
            v = x["new_tsc"] * 1000 / r
            earn.append((day, v))
            if day in p1440:
                earn_usd.append((day, v * p1440[day]))
    earn_card = f"""<section class="card"><h2>What does mining earn?</h2><p class="sub">What 1 K proof/s would earn running for a whole UTC day at that day's difficulty, before pool fees, electricity and luck. Multiply by your miner's speed. USD uses that day's SafeTrade close — it shows scale, not income anyone can count on.</p>
<div class="two"><div><b>TSC / day</b>{svg_line([{"name": "TSC per 1 K proof/s", "points": earn[-60:], "color": PALETTE[1]}], width=560, yfmt=lambda v: fnum(v, 2))}</div><div><b>USD / day</b>{svg_line([{"name": "USD per 1 K proof/s", "points": earn_usd[-60:], "color": PALETTE[2]}], width=560, yfmt=lambda v: fusd(v, 2))}</div></div>
<p class="note">The reward falls when more miners join and rises when they leave; the second driver is the shrinking block reward (epochs).</p></section>"""

    mults = [b[5] for b in c["24h"]["_list"]]
    avg_m = sum(mults) / len(mults) if mults else None
    poi_card = f"""<section class="card"><h2>Proof-of-inference — work multiplier</h2><p class="sub">In TSC a block can count as more work than its bits imply if the miner attached a “useful” inference proof (multiplier &gt; 1). A high average multiplier means miners are actually running inference, not just hashing.</p>
<div class="kpis"><div class="kpi"><div class="k">Average multiplier, 24h</div><div class="v">×{fnum(avg_m,3) if avg_m else '—'}</div><div class="s">max in 24h: ×{fnum(max(mults),3) if mults else '—'}</div></div>
<div class="kpi"><div class="k">Blocks with multiplier &gt; 1</div><div class="v">{fpct(sum(1 for m in mults if m > 1.0001)/len(mults)*100 if mults else None,0,False)}</div><div class="s">of {fnum(len(mults))} blocks in 24h</div></div></div></section>"""

    cost_card = costs.mining_card(sys.modules[__name__], c['costs'], price)
    return hero + kpis + miners.epoch_card(sys.modules[__name__], c.get('epoch'), price) + rate_card + cost_card + who_card + stack_card + diff_card + earn_card + poi_card


# ---------------------------------------------------------------- page: Holders

def build_holders(d, c):
    hs = d["holders"]
    circ = snap_val(d, "circ_supply")
    price = snap_val(d, "price_usdt")
    if not hs:
        return '<section class="card"><h2>Largest holders</h2><p class="sub">No holder snapshot yet — it appears after the first full collector run.</p></section>'
    tot100 = sum(h[2] for h in hs)
    acc7 = sum(1 for h in hs if (h[3] or 0) > 0)
    dist7 = sum(1 for h in hs if (h[3] or 0) < 0)
    net7 = sum((h[3] or 0) for h in hs)
    net30 = sum((h[4] or 0) for h in hs)
    hero = f"""<section class="hero"><h1>Largest <em>TSC holders</em>.</h1>
<p class="lead">Top {len(hs)} addresses by balance with 7- and 30-day net flow. This is the seed of a “who's accumulating” view — without exchange labels an exchange wallet cannot yet be told apart from an investor, so read it as a watchlist.</p>
<div class="snap">Holder snapshot: <b>{fdt(d['holders_ts'])}</b> · balances and flows from the explorer</div>
<div class="grid">
<div class="cell"><div class="k">Top {len(hs)} addresses hold</div><div class="v">{fpct(tot100/circ*100 if circ else None,1,False)}</div><div class="s">{fshort(tot100)} TSC of {fshort(circ)} circulating</div></div>
<div class="cell"><div class="k">Net flow, 7 days (top {len(hs)})</div><div class="v {cls_pct(net7)}">{('+' if net7 > 0 else '')+fshort(net7,1)} TSC</div><div class="s">30 days: {('+' if net30 > 0 else '')+fshort(net30,1)} TSC</div></div>
<div class="cell"><div class="k">Accumulating / distributing, 7d</div><div class="v"><span class="up">{acc7}</span> / <span class="down">{dist7}</span></div><div class="s">addresses with positive / negative net flow</div></div>
</div></section>"""
    rows = []
    for r, a, bal, n7, n30, sh, txc, last in hs:
        tag = f' <span class="badge">{esc(d["aliases"][a])}</span>' if a in d["aliases"] else ""
        rows.append(f"""<tr><td>{r}</td><td class="mono">{esc(short_addr(a))}{tag}</td><td>{fnum(bal,0)}</td><td>{fusd(bal*price,0) if price else '—'}</td><td>{fnum(sh,2)}%</td>
<td class="{cls_pct(n7)}">{signed(n7)}</td><td class="{cls_pct(n30)}">{signed(n30)}</td><td>{fnum(txc)}</td><td>{fdt(last,'%d %b %H:%M')}</td></tr>""")
    table = f"""<section class="card"><h2>Top {len(hs)} addresses</h2><p class="sub">Balance in TSC, value at the current price, share of circulating supply, net flow (inflows − outflows) over 7 and 30 days.</p>
<div style="overflow:auto"><table><thead><tr><th>#</th><th>Address</th><th>Balance, TSC</th><th>Value</th><th>Share</th><th>Net 7d</th><th>Net 30d</th><th>Tx</th><th>Last activity</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<p class="note">Pool addresses (Tigerpool, Luckypool) are payout wallets, not investors. Next step: exchange labels (SafeTrade) and clustering of linked wallets — only then can “withdrawn from the exchange” be counted the way quantus.watch does.</p></section>"""
    return hero + table


# ---------------------------------------------------------------- page: About the data

def build_about(d, c):
    mn = d["meta"]
    return f"""<section class="card"><h2>About the data</h2><p class="sub">Definitions, sources and limitations — so that every number can be reproduced.</p>
<h3>Sources</h3><dl>
<dt>Blocks</dt><dd><code>tscscan.xyz/api/blocks</code> — height, time, miner address, base and effective difficulty, PoI multiplier. In the database: {fnum(c['all']['blocks'])} blocks (0–{fnum(c['tip_h'])}), full history since genesis ({fdt(c['genesis_ts'],'%d %b %Y')}).</dd>
<dt>Price and volume</dt><dd>SafeTrade public k-line API (1h and 1d candles, TSC/USDT pair, 1 USDT ≈ $1). A thin, unsanctioned exchange — the only one with this pair. Current price and 24h volume also from <code>tscscan.xyz/api/market-summary</code>.</dd>
<dt>Supply, holders, concentration, daily activity</dt><dd><code>tscscan.xyz/api/analytics</code> and <code>/api/home</code>; top addresses from <code>/api/holders</code>; pool aliases from <code>/api/address-aliases</code>.</dd>
</dl>
<h3>Definitions</h3><dl>
<dt>Work rate</dt><dd>Sum of effective block difficulty in the window ÷ window length in seconds (proof/s). Same method as the explorer (checked: over 24h the only difference comes from the window end).</dd>
<dt>Pool share</dt><dd>Blocks found by an address ÷ all blocks in the window. Over 24h, ±3–5 points is random noise.</dd>
<dt>Pool work rate</dt><dd>Sum of effective difficulty of its blocks ÷ 86,400 s.</dd>
<dt>New TSC per day</dt><dd>Blocks in the window × block reward for the epoch (the reward drops 40% each epoch; currently {fnum(reward_at(c['epochs'], c['tip_h']),4)} TSC).</dd>
<dt>Issuance vs. volume</dt><dd>New TSC over 24h × current price ÷ 24h volume in USD. Shows how much of the turnover miner reward sales alone could account for.</dd>
<dt>Reward per 1 K proof/s</dt><dd>New TSC in the day × (1000 ÷ the day's work rate). Before pool fees (~1%), electricity and variance.</dd>
{costs.about_dl()}
<dt>Intelligence multiplier</dt><dd>Proof-v4 price of each block (proof.multiplier, 1–12×): the header target is divided by it, so a 1.2× block needs 20% more work. Mean per block finder since block 26,950; "blocks lost" = blocks × (mean − 1).</dd>
<dt>Pools needed for a majority</dt><dd>The smallest number of addresses whose combined 7-day share of blocks exceeds 50%.</dd>
<dt>Days and times</dt><dd>Daily aggregates use UTC days; all timestamps are shown in UTC.</dd>
</dl>
<h3>Limitations</h3>
<p>Unlabelled miner addresses are not necessarily solo — they may be pools without an alias. Share of blocks approximates share of work only over a large sample. Top-holder flows come from the explorer and do not distinguish exchanges from investors. A price from one thin exchange can be easy to move. This dashboard is not investment advice.</p>
<h3>Collector status</h3><dl><dt>Last collection</dt><dd>{fdt(int(mn.get('last_collect_ts',0)))}</dd><dt>Snapshots in database</dt><dd>{fnum(len(d['snaps']))}</dd><dt>Price candles</dt><dd>{fnum(len(d['p60']))} × 1h, {fnum(len(d['p1440']))} × 1d</dd></dl>
<p class="note">Code: <code>collect.py</code> (collection into SQLite) and <code>build.py</code> (generates these pages). Data export: <a href="data.json">data.json</a>.</p></section>"""


# ---------------------------------------------------------------- official links

LINKS = [
    ("Website & Docs", [
        ("Website", "https://tensorcash.org/", "Project homepage"),
        ("Verifiable-inference whitepaper", "https://tensorcash.org/whitepapers/verifiable-inference/", "How proof-of-inference works"),
        ("Block explorer", "https://explorer.tensorcash.org/", "Official explorer"),
        ("Release verification", "https://verify.tensorcash.org/", "Check release hashes and signatures"),
    ]),
    ("Mining & Running the Network", [
        ("How to mine", "https://tensorcash.org/blog/how-to-mine/", "Official mining guide"),
        ("How to run a node", "https://tensorcash.org/blog/how-to-run-a-node/", "Full node setup"),
        ("How to run a verifier", "https://tensorcash.org/blog/how-to-run-a-verifier/", "Verifier setup"),
        ("How to run the wallet", "https://tensorcash.org/blog/how-to-run-the-wallet/", "Wallet setup"),
        ("Latest releases", "https://git.tensorcash.org/tensorcash/tensorcash/releases", "Always verify signatures before running"),
    ]),
    ("Socials", [
        ("X", "https://x.com/Tensorcash", "@Tensorcash"),
        ("Telegram", "https://t.me/TensorCash_org", "t.me/TensorCash_org"),
        ("Reddit", "https://reddit.com/r/TensorCash", "r/TensorCash"),
        ("Nostr", "https://njump.me/npub1pft0lcdaznczfhjflu5n3t3j7argg355aj2dhm8u2t9avmdwwnfqlp0kv6", "npub1pft0lcd…lp0kv6"),
        ("Source code", "https://git.tensorcash.org/tensorcash", "git.tensorcash.org"),
        ("Discord", "https://discord.gg/xR6CjKm4EY", "Official Discord server"),
    ]),
]


def build_links(d, c):
    from urllib.parse import urlparse
    groups = "".join(
        f'<section class="card"><h2>{esc(g)}</h2><div class="links">'
        + "".join(f'<a class="lnk" href="{esc(u)}" target="_blank" rel="noopener noreferrer nofollow">'
                  f'<span class="lt">{esc(t)} <span class="ext">↗</span></span><span class="ld">{esc(desc)}</span>'
                  f'<span class="lu">{esc(urlparse(u).netloc.removeprefix("www."))}</span></a>' for t, u, desc in items)
        + "</div></section>" for g, items in LINKS)
    return (f'<section class="card"><h2>Official TensorCash links</h2><p class="sub">Project websites, guides, releases and community channels. '
            f'tsc.watch is an independent dashboard and is not affiliated with the TensorCash team. Before downloading software, check the domain in your '
            f'address bar and verify release signatures at <a href="https://verify.tensorcash.org/" target="_blank" rel="noopener noreferrer nofollow">verify.tensorcash.org</a>. '
            f'Never share your seed phrase, and treat unsolicited DMs offering “support” as scams.</p></section>' + groups)


# ---------------------------------------------------------------- JSON export

def export_json(d, c):
    w24, w7 = c["24h"], c["7d"]
    return {
        "generated_at": c["now"], "tip_height": c["tip_h"], "tip_timestamp": c["tip_ts"],
        "price_usdt": snap_val(d, "price_usdt"), "volume_24h_usdt": snap_val(d, "volume_24h_usdt"),
        "market_cap": snap_val(d, "market_cap"), "circ_supply": snap_val(d, "circ_supply"), "holders": snap_val(d, "holders"),
        "work_rate_24h": w24["rate"], "work_rate_1h": c["1h"]["rate"], "blocks_24h": w24["blocks"],
        "block_time_24h": w24["block_time"], "new_tsc_24h": w24["new_tsc"],
        "miners_24h": [{"address": a, "alias": d["aliases"].get(a), "blocks": m["blocks"], "share": m["blocks"] / w24["blocks"] * 100 if w24["blocks"] else 0}
                       for a, m in sorted(w24["miners"].items(), key=lambda kv: -kv[1]["blocks"])],
        "miners_7d": [{"address": a, "alias": d["aliases"].get(a), "blocks": m["blocks"], "share": m["blocks"] / w7["blocks"] * 100 if w7["blocks"] else 0}
                      for a, m in sorted(w7["miners"].items(), key=lambda kv: -kv[1]["blocks"])],
        "mining_cost": costs.export(c["costs"]),
        "reward_epoch": c.get("epoch"),
        "daily": [{"day": day, "blocks": x["blocks"], "work_rate": x["work"] / DAY, "new_tsc": x["new_tsc"],
                   "avg_difficulty": x["diff"] / x["blocks"] if x["blocks"] else None} for day, x in c["daily"].items()],
    }


# ---------------------------------------------------------------- main

def main():
    con = sqlite3.connect(DB_PATH)
    d = load(con)
    c = compute(d)
    c["costs"] = costs.compute_costs(d, c, reward_at)
    price = snap_val(d, "price_usdt") or (d["p60"][-1][4] if d["p60"] else None)
    mc = miners.compute(con, d, c, reward_at, price)
    ep = miners.epoch_info(c, reward_at)
    c["epoch"] = ep
    agg = guides.compute_agg(d["proof"], c["tip_h"], c["tip_ts"])
    os.makedirs(SITE, exist_ok=True)
    gen = int(time.time())
    pages = {
        "index.html": ("TSC market", "market", build_market(d, c)),
        "mining.html": ("TSC mining", "mining", build_mining(d, c)),
        "proof.html": ("TSC proof efficiency", "proof", guides.proof_page(sys.modules[__name__], agg, d["aliases"], price, reward_at(c["epochs"], c["tip_h"])) if agg else "<section class=card>No proof-v4 data yet.</section>"),
        "upgrades.html": ("TSC protocol upgrades", "upgrades", guides.upgrades_page(sys.modules[__name__], agg or {"buckets": []}, c["tip_h"], c["tip_ts"], c["7d"]["block_time"] or 600, d["releases"])),
        "contact.html": ("Contact tsc.watch", "contact", guides.contact_page(sys.modules[__name__], os.environ.get("CONTACT_FORM_KEY", "").strip())),
        "start.html": ("Start mining TSC", "start", guides.start_page(sys.modules[__name__], c["24h"]["rate"], c["24h"]["new_tsc"], price)),
        "miner.html": ("TSC miner dashboard & pools", "miners", miners.build_miner_page(sys.modules[__name__], mc, ep, c)),
        "calc.html": ("TSC mining calculator", "calc", miners.build_calc_page(sys.modules[__name__], c, ep, price)),
        "holders.html": ("TSC holders", "holders", build_holders(d, c)),
        "links.html": ("Official TensorCash links", "links", build_links(d, c)),
        "about.html": ("About the data", "about", build_about(d, c)),
    }
    for fn, (title, key, body) in pages.items():
        with open(os.path.join(SITE, fn), "w", encoding="utf-8") as f:
            f.write(page(title, key, body, gen, c["tip_h"], c["tip_ts"], fn))
    # translated copies: /zh/*.html and /ru/*.html (+ dictionaries for text created by JS)
    missing = {}
    for lang in i18n.LANGS:
        if lang == "en":
            continue
        os.makedirs(os.path.join(SITE, lang), exist_ok=True)
        dic, miss = i18n.load(lang), set()
        for fn in pages:
            with open(os.path.join(SITE, fn), encoding="utf-8") as f:
                en = f.read()
            i18n.translate_html(en, dic, miss, lang)
            with open(os.path.join(SITE, lang, fn), "w", encoding="utf-8") as f:
                f.write(i18n.localize(en, lang, fn, site_domain()))
        missing[lang] = len(miss)
    i18n.write_runtime(SITE)
    with open(os.path.join(SITE, "data.json"), "w", encoding="utf-8") as f:
        json.dump(export_json(d, c), f, ensure_ascii=False, indent=1)
    with open(os.path.join(SITE, "miners.json"), "w", encoding="utf-8") as f:
        json.dump(miners.export_json(mc, c, agg), f, separators=(",", ":"))
    open(os.path.join(SITE, ".nojekyll"), "w").close()
    dom = site_domain()
    if dom:
        with open(os.path.join(SITE, "CNAME"), "w") as f:
            f.write(dom + "\n")
    with open(os.path.join(SITE, "robots.txt"), "w") as f:
        f.write("User-agent: *\nAllow: /\n")
    print(time.strftime("%Y-%m-%d %H:%M:%S"), f"build ok → {SITE} (block {c['tip_h']}, {len(pages)} pages, untranslated strings: {missing})")


if __name__ == "__main__":
    main()
