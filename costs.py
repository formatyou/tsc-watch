"""
tsc-watch — cost to mine 1 TSC.

Method: a GPU with rate R (PoI/s) on a network doing N PoI/s earns R/N of the
TSC issued in the window. Cost of one TSC = GPU running cost for the window
÷ TSC that GPU earns in it. Network rate and issuance come from the chain;
per-GPU rates from the suprnova pool's published benchmark table (same PoI/s
unit as the explorer's network work rate).

All assumptions live in this file so they are easy to review and update.
"""

ELEC_USD_KWH = 0.10          # electricity price used for "own GPU" scenarios
POOL_FEE = 0.0               # shown separately in the text; not deducted
MIN_NET_RATE = 1000          # ignore days when the network was essentially idle

# name, PoI/s (suprnova benchmark table), board power in W (vendor TDP),
# cloud rental USD/h or None (fixed reference rates, updated by hand)
GPUS = [
    ("B200", 56.0, 1000, None),
    ("H100 / H200", 40.0, 700, 1.00),
    ("RTX PRO 6000 Blackwell", 26.0, 600, None),
    ("RTX 5090", 18.6, 575, 0.26),
    ("RTX A6000 / A40", 9.5, 300, None),
]
REF_GPU = "RTX 5090"
RATES_SOURCE = "tsc.suprnova.cc (Start Mining → rates table)"
RENT_SOURCE = "cheapest spot listings on Vast.ai, Sep 2026"
RATES_DATE = "29 Sep 2026"
DAY = 86400


def _gpu(name):
    return next(g for g in GPUS if g[0] == name)


def tsc_per_day(new_tsc_day, net_rate, poi):
    """TSC one GPU earns per day given network issuance per day and network rate."""
    if not net_rate or net_rate <= 0:
        return None
    return new_tsc_day * poi / net_rate


def cost_elec(tsc_day, watts, usd_kwh=ELEC_USD_KWH):
    if not tsc_day:
        return None
    return watts / 1000 * 24 * usd_kwh / tsc_day


def cost_rent(tsc_day, usd_h):
    if not tsc_day or usd_h is None:
        return None
    return usd_h * 24 / tsc_day


def _window(blocks, end, sec, reward_at, epochs):
    w = [b for b in blocks if end - sec < b[1] <= end]
    work = sum(b[3] for b in w)
    new = sum(reward_at(epochs, b[0]) for b in w)
    return new * DAY / sec, work / sec  # issuance per day, network rate


def compute_costs(d, c, reward_at):
    """Returns dict with current, lagged and daily-history cost figures."""
    blocks, epochs = d["blocks"], c["epochs"]
    ref = _gpu(REF_GPU)
    out = {"gpus": [], "history": []}

    # current (last 24h, same window as the rest of the site)
    new_day, rate = c["24h"]["new_tsc"], c["24h"]["rate"]
    for name, poi, watts, rent in GPUS:
        t = tsc_per_day(new_day, rate, poi)
        out["gpus"].append({"name": name, "poi": poi, "watts": watts, "rent_h": rent,
                            "tsc_day": t, "elec": cost_elec(t, watts), "rent": cost_rent(t, rent),
                            "eff": poi / watts * 1000})
    t_ref = tsc_per_day(new_day, rate, ref[1])
    out["now"] = {"tsc_day": t_ref, "elec": cost_elec(t_ref, ref[2]), "rent": cost_rent(t_ref, ref[3]),
                  "h100_rent": cost_rent(tsc_per_day(new_day, rate, 40.0), 1.00)}

    # same metric 1 and 7 days ago (24h windows ending then)
    for key, lag in (("d1", DAY), ("d7", 7 * DAY)):
        nd, nr = _window(blocks, c["ref"] - lag, DAY, reward_at, epochs)
        t = tsc_per_day(nd, nr, ref[1])
        out[key] = cost_elec(t, ref[2])

    # daily history (complete UTC days only)
    closes = {r[0]: r[4] for r in d["p1440"]}
    today = c["ref"] // DAY * DAY
    for day, x in c["daily"].items():
        if day >= today:
            continue
        nr = x["work"] / DAY
        if nr < MIN_NET_RATE:
            continue
        t = tsc_per_day(x["new_tsc"], nr, ref[1])
        t_h = tsc_per_day(x["new_tsc"], nr, 40.0)
        out["history"].append({"day": day, "net_rate": nr, "tsc_day": t,
                               "elec": cost_elec(t, ref[2]), "rent": cost_rent(t, ref[3]),
                               "h100_rent": cost_rent(t_h, 1.00), "price": closes.get(day)})
    return out


# ---------------------------------------------------------------- rendering

def _delta(B, now, then):
    if not now or not then:
        return "—"
    p = (now / then - 1) * 100
    return B.fpct(p, 1 if abs(p) < 10 else 0)


def market_tile(B, cs, price):
    now = cs["now"]["elec"]
    mult = price / now if now and price else None
    return (f'<div class="kpi"><div class="k">Cost to mine 1 TSC</div><div class="v">{B.fusd(now, 2)}</div>'
            f'<div class="s">{REF_GPU}, electricity ${ELEC_USD_KWH:.2f}/kWh · price is {B.fnum(mult, 1) if mult else "—"}× cost · '
            f'<a href="mining.html#cost">details</a></div></div>')


def mining_card(B, cs, price):
    now = cs["now"]
    mult = price / now["elec"] if now["elec"] and price else None
    rent_mult = price / now["rent"] if now["rent"] and price else None
    hist = cs["history"]
    kpis = f"""<div class="kpis">
<div class="kpi"><div class="k">Own {REF_GPU}, electricity only</div><div class="v">{B.fusd(now['elec'], 2)}</div><div class="s">per TSC · vs 1 day ago {_delta(B, now['elec'], cs['d1'])} · vs 7 days ago {_delta(B, now['elec'], cs['d7'])}</div></div>
<div class="kpi"><div class="k">Rented {REF_GPU} (${_gpu(REF_GPU)[3]:.2f}/h)</div><div class="v">{B.fusd(now['rent'], 2)}</div><div class="s">per TSC · price is {B.fnum(rent_mult, 2) if rent_mult else '—'}× this cost</div></div>
<div class="kpi"><div class="k">Rented H100 ($1.00/h)</div><div class="v">{B.fusd(now['h100_rent'], 2)}</div><div class="s">per TSC · datacenter-grade marginal miner</div></div>
<div class="kpi"><div class="k">TSC price vs electricity cost</div><div class="v">{B.fnum(mult, 1) if mult else '—'}×</div><div class="s">{B.fusd(price)} market vs {B.fusd(now['elec'], 2)} cost · one {REF_GPU} earns {B.fnum(now['tsc_day'], 2)} TSC/day</div></div>
</div>"""
    chart = B.svg_line([
        {"name": "TSC price (daily close)", "points": [(h["day"], h["price"]) for h in hist if h["price"]], "color": "#101828", "area": False, "width": 2.2},
        {"name": f"cost: rented H100 $1.00/h", "points": [(h["day"], h["h100_rent"]) for h in hist], "color": B.PALETTE[3], "area": False, "dash": True},
        {"name": f"cost: rented {REF_GPU} ${_gpu(REF_GPU)[3]:.2f}/h", "points": [(h["day"], h["rent"]) for h in hist], "color": B.PALETTE[1], "area": False},
        {"name": f"cost: own {REF_GPU}, ${ELEC_USD_KWH:.2f}/kWh", "points": [(h["day"], h["elec"]) for h in hist], "color": B.PALETTE[0], "area": True},
    ], yfmt=lambda v: B.fusd(v, 2), height=300)
    rows = "".join(
        f"<tr><td>{B.esc(g['name'])}{' <span class=badge>reference</span>' if g['name'] == REF_GPU else ''}</td><td>{B.fnum(g['poi'], 1)}</td><td>{B.fnum(g['watts'])} W</td>"
        f"<td>{B.fnum(g['eff'], 1)}</td><td>{B.fnum(g['tsc_day'], 2)}</td><td>{B.fusd(g['elec'], 2)}</td>"
        f"<td>{('$' + format(g['rent_h'], '.2f') + '/h') if g['rent_h'] is not None else '—'}</td><td>{B.fusd(g['rent'], 2) if g['rent'] else '—'}</td></tr>"
        for g in cs["gpus"])
    first = hist[0] if hist else None
    return f"""<section class="card" id="cost"><h2>What does it cost to mine 1 TSC?</h2><p class="sub">GPU running cost ÷ the TSC that GPU earns at the current network work rate. A GPU doing R PoI/s on a network doing N PoI/s earns R/N of all new TSC. Rising network work rate and falling block rewards push this cost up; if the price falls below it, miners with that setup mine at a loss.</p>
{kpis}
<h3>Cost to mine 1 TSC vs. price, per UTC day</h3>{chart}
<p class="note">History starts {B.fdt(first['day'], '%d %b %Y') if first else '—'}, the first day the network exceeded {B.fnum(MIN_NET_RATE)} PoI/s. Days before the SafeTrade listing have no price line.</p>
<h3>By GPU, last 24 hours</h3>
<div style="overflow:auto"><table><thead><tr><th>GPU</th><th>PoI/s</th><th>Power</th><th>PoI/s per kW</th><th>TSC / day</th><th>Electricity cost / TSC</th><th>Rent</th><th>Rental cost / TSC</th></tr></thead><tbody>{rows}</tbody></table></div>
<p class="note">Estimates, not quotes. Power uses the vendor board limit (TDP); real draw during inference is often lower, so electricity cost is a conservative upper bound. Not included: pool fee (~1%), hardware depreciation, cooling, luck. PoI/s from {RATES_SOURCE}, {RATES_DATE}; rental from {RENT_SOURCE}.</p></section>"""


def about_dl():
    gl = "; ".join(f"{n} {p} PoI/s @ {w} W" + (f", ${r:.2f}/h" if r is not None else "") for n, p, w, r in GPUS)
    return (f"<dt>Cost to mine 1 TSC</dt><dd>GPU cost per day ÷ TSC that GPU earns per day, where TSC per day = new TSC per day × GPU PoI/s ÷ network PoI/s. "
            f"Electricity scenario: board power × 24 h × ${ELEC_USD_KWH:.2f}/kWh. Rental scenario: fixed hourly rate × 24 h. "
            f"Reference GPU {REF_GPU}. Assumptions: {gl}. Rates from {RATES_SOURCE} ({RATES_DATE}); rental from {RENT_SOURCE}. "
            f"History uses complete UTC days with network rate ≥ {MIN_NET_RATE:,} PoI/s.</dd>")


def export(cs):
    return {"reference_gpu": REF_GPU, "electricity_usd_kwh": ELEC_USD_KWH,
            "now": cs["now"], "prev_1d_elec": cs["d1"], "prev_7d_elec": cs["d7"],
            "gpus": cs["gpus"], "history": cs["history"]}
