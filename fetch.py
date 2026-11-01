"""BC electricity trade dashboard: hourly BC-US and BC-AB intertie flows with estimated wholesale value.

Flows (MW, + = BC exporting), best source wins per hour:
  1. BC Hydro hourly tie-line data (official XLS, published ~monthly)        src "bch"
  2. EIA Hourly Electric Grid Monitor, BPAT->BCHA interchange (US tie only)  src "eia"
  3. BC Hydro 5-minute actual flow page (rolling 7 days), averaged hourly    src "bch5"
  4. AESO settled import/export volumes on the BC path (AB tie, gap fill)   src "aeso"  -> ESTIMATE
Prices: AESO hourly pool price (C$) for BC-AB; EIA/ICE Mid-C day-ahead PEAK price (US$) for BC-US,
applied to every hour of the delivery day, converted with Bank of Canada daily USD/CAD.
Dollar values are market-price ESTIMATES (MWh x hub price), not BC Hydro/Powerex settlements.

Usage: python fetch.py            # hourly incremental refresh (trailing ~10 days)
       python fetch.py --backfill # one-time rebuild from 2025-01-01
"""
import os, sys, io, re, json, datetime as dt, urllib.request, urllib.parse
import pandas as pd

EIA_KEY = os.environ.get("EIA_API_KEY", "DEMO_KEY")
AESO_KEY = os.environ.get("AESO_API_KEY")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36 bc-powerflow"}
TZ = "America/Vancouver"
START = pd.Timestamp("2025-01-01", tz=TZ)
D = "docs"
HOURLY, PRICES, META = f"{D}/hourly.csv", f"{D}/daily_prices.csv", f"{D}/meta.json"
BCH = "https://www.bchydro.com"
META_BIAS = {}
RANK = {"bch": 3, "eia": 2, "bch5": 1, "aeso": 0}


def check_tz():
    # BC moved to permanent UTC-7 in Nov 2026; needs tzdata >= 2026 (workflow pins the tzdata package).
    off = pd.Timestamp("2026-12-15 12:00").tz_localize(TZ).utcoffset()
    if off != pd.Timedelta(hours=-7):
        log(f"WARNING: tz database gives {TZ} offset {off} for Dec 2026; expected -07:00 (update tzdata)")


def get(url, headers=None, timeout=90):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    return urllib.request.urlopen(req, timeout=timeout).read()


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- storage
def load_hourly():
    if os.path.exists(HOURLY):
        h = pd.read_csv(HOURLY, dtype={"us_src": str, "ab_src": str})
        h.index = pd.to_datetime(h.pop("t_utc"), utc=True)
        return h
    return pd.DataFrame(columns=["us_mw", "us_src", "ab_mw", "ab_src", "pool_cad"],
                        index=pd.DatetimeIndex([], tz="UTC"))


def merge_flow(h, s, col, src):
    """s: Series of MW indexed by UTC hour-beginning. Overwrite where new source ranks >= existing."""
    s = s.dropna()
    if s.empty:
        return h
    h = h.reindex(h.index.union(s.index))
    cur = h.loc[s.index, f"{col}_src"]
    ok = cur.isna() | (cur.map(RANK).fillna(-1) <= RANK[src])
    idx = s.index[ok.values]
    h.loc[idx, f"{col}_mw"] = s[idx].astype(float).values
    h.loc[idx, f"{col}_src"] = src
    log(f"  {col}<-{src}: {len(idx)} of {len(s)} hours")
    return h


# ---------------------------------------------------------------- BC Hydro
def bch_xls_links():
    html = get(f"{BCH}/energy-in-bc/operations/transmission/transmission-system/actual-flow-data/historical-data.html").decode("utf8", "ignore")
    links = {}
    for href in re.findall(r'href="([^"]*HourlyTielineData ?(\d{4})\.xls)"', html):
        links[int(href[1])] = BCH + urllib.parse.quote(href[0])
    return links


def parse_bch_xls(raw):
    t = pd.read_excel(io.BytesIO(raw), header=1)
    t = t.dropna(subset=["Date", "HE"])
    t["Date"] = pd.to_datetime(t["Date"], format="mixed")
    out = []
    for day, g in t.groupby("Date", sort=True):
        # local midnight -> UTC, then consecutive hours (handles 23/25-hour DST days)
        t0 = pd.Timestamp(day).tz_localize(TZ).tz_convert("UTC")
        idx = [t0 + pd.Timedelta(hours=i) for i in range(len(g))]
        out.append(pd.DataFrame({"us": pd.to_numeric(g["US Tielines"], errors="coerce").values,
                                 "ab": pd.to_numeric(g["AB Tielines"], errors="coerce").values}, index=idx))
    return pd.concat(out)


def bch_5min():
    html = get(f"{BCH}/bctc/system_cms/actual_flow/data.aspx").decode("latin1")
    text = re.sub(r"<[^>]+>", " ", html).replace("\x00", " ")
    rows = re.findall(r"(\d{2}-[A-Za-z]{3}-\d{2} \d{2}:\d{2}:\d{2})\s+(-?\d+)\s+(-?\d+)", text)
    df = pd.DataFrame(rows, columns=["ts", "us", "ab"])
    ts = pd.to_datetime(df["ts"], format="%d-%b-%y %H:%M:%S")
    ts = ts.dt.tz_localize(TZ, ambiguous="infer", nonexistent="shift_forward").dt.tz_convert("UTC")
    df.index = pd.DatetimeIndex(ts)
    df = df.drop(columns="ts")
    df[["us", "ab"]] = df[["us", "ab"]].astype(float)
    # Since the Nov 2026 DST change the table labels lag real time by 1 h (latest_values.txt carries the
    # correct time for the same last sample). Align rows after the last DST repeat to latest_values.txt.
    try:
        lv = get(f"{BCH}/bctc/system_cms/actual_flow/latest_values.txt").decode("latin1").strip().splitlines()[-1].split(",")
        lt = pd.Timestamp(dt.datetime.strptime(lv[0], "%d-%b-%y %H:%M:%S")).tz_localize(TZ, ambiguous=False).tz_convert("UTC")
        last = df.iloc[-1]
        off = (lt - df.index[-1]).round("h")
        if abs(off) >= pd.Timedelta(hours=1) and float(lv[1]) == last["us"] and float(lv[2]) == last["ab"]:
            back = [i for i in range(1, len(df)) if df.index[i] <= df.index[i - 1]]
            k = back[-1] if back else 0
            df.index = df.index[:k].append(df.index[k:] + off)
            df = df[~df.index.duplicated(keep="last")]
            log(f"  5-min table: shifted {len(df) - k} rows by {off}")
    except Exception as e:
        log("  latest_values check failed:", e)
    # 5-min sample at hh:05..(hh+1):00 belongs to hour beginning hh
    hour = (df.index - pd.Timedelta(seconds=1)).floor("h")
    g = df.groupby(hour)
    m = g[["us", "ab"]].mean()
    m = m[g.size() >= 10]  # complete-ish hours only
    latest = {"t_utc": df.index[-1].isoformat(), "us_mw": float(df["us"].iloc[-1]), "ab_mw": float(df["ab"].iloc[-1])}
    return m, latest


# ---------------------------------------------------------------- EIA
def eia_us(start_utc):
    rows, off = [], 0
    while True:
        url = ("https://api.eia.gov/v2/electricity/rto/interchange-data/data/?api_key=%s&frequency=hourly"
               "&data[0]=value&facets[fromba][]=BPAT&facets[toba][]=BCHA&start=%s"
               "&sort[0][column]=period&sort[0][direction]=asc&offset=%d&length=5000") % (EIA_KEY, start_utc.strftime("%Y-%m-%dT%H"), off)
        r = json.loads(get(url.replace("[", "%5B").replace("]", "%5D")))["response"]
        rows += r["data"]
        off += 5000
        if off >= int(r["total"]):
            break
    if not rows:
        return pd.Series(dtype=float)
    df = pd.DataFrame(rows)
    # EIA period is UTC hour-ENDING; value = BPAT->BCHA, so BC export = -value
    t = pd.to_datetime(df["period"], format="%Y-%m-%dT%H", utc=True) - pd.Timedelta(hours=1)
    return pd.Series(-pd.to_numeric(df["value"]).values, index=t).groupby(level=0).sum()


# ---------------------------------------------------------------- AESO
def aeso_get(path):
    return json.loads(get(f"https://apimgw.aeso.ca/public/{path}", {"API-KEY": AESO_KEY}))


def aeso_pool(start_date, end_date):
    out = {}
    d = start_date
    while d <= end_date:
        e = min(end_date, d + dt.timedelta(days=364))
        for r in aeso_get(f"poolprice-api/v1.1/price/poolPrice?startDate={d}&endDate={e}")["return"]["Pool Price Report"]:
            if r["pool_price"] not in (None, ""):
                out[pd.Timestamp(r["begin_datetime_utc"], tz="UTC")] = float(r["pool_price"])
        d = e + dt.timedelta(days=1)
    return pd.Series(out, dtype=float)


def aeso_bc_sched(day):
    """AB-side settled volumes for importers on the BC path (*BC) minus exporters (*XB), by hour. + = into AB = BC export."""
    j = aeso_get(f"meteredvolume-api/v1/meteredvolume/details?startDate={day}")["return"]
    net, tot = {}, 0.0
    for x in j:
        for a in x["asset_list"]:
            aid, c = a["asset_ID"], a["asset_class"]
            sign = 1 if (c == "IMPORTER" and (aid.endswith("BC") or aid == "ABCP")) else -1 if (c == "EXPORTER" and aid.endswith("XB")) else 0
            if not sign:
                continue
            for m in a["metered_volume_list"]:
                v = float(m["metered_volume"] or 0)
                t = pd.Timestamp(m["begin_date_utc"], tz="UTC")
                net[t] = net.get(t, 0.0) + sign * v
                tot += v
    return pd.Series(net, dtype=float) if tot > 0 else pd.Series(dtype=float)


def aeso_snapshot():
    csd = aeso_get("currentsupplydemand-api/v2/csd/summary/current")["return"]
    ties = {i["path"]: -i["actual_flow"] for i in csd["interchange_list"]}
    return {"as_of_mpt": csd["effective_datetime_mpt"], "bc_to_ab_mw": ties.get("British Columbia"),
            "ties_into_ab_mw": ties}


# ---------------------------------------------------------------- prices
def midc_daily(years):
    recs = []
    for y in years:
        url = (f"https://www.eia.gov/electricity/wholesale/xls/ice_electric-{y}.xlsx" if y == dt.date.today().year
               else f"https://www.eia.gov/electricity/wholesale/xls/archive/ice_electric-{y}final.xlsx")
        df = pd.read_excel(io.BytesIO(get(url)))
        df.columns = [re.sub(r"\s+", " ", str(c)).strip() for c in df.columns]
        m = df[df["Price hub"].astype(str).str.contains("Mid C Peak", case=False)]
        for _, r in m.iterrows():
            for day in pd.date_range(pd.Timestamp(r["Delivery start date"]), pd.Timestamp(r["Delivery end date"])):
                recs.append((day.date(), float(r["Wtd avg price $/MWh"]), pd.Timestamp(r["Trade date"]).date()))
    s = pd.DataFrame(recs, columns=["date", "midc_usd", "trade"]).sort_values("trade").drop_duplicates("date", keep="last")
    return s.set_index("date")["midc_usd"]


def fx_daily(start):
    o = json.loads(get(f"https://www.bankofcanada.ca/valet/observations/FXUSDCAD/json?start_date={start}"))["observations"]
    return pd.Series({pd.Timestamp(x["d"]).date(): float(x["FXUSDCAD"]["v"]) for x in o if x.get("FXUSDCAD")})


def update_prices(full):
    old = pd.read_csv(PRICES, parse_dates=["date"]) if os.path.exists(PRICES) and not full else None
    today = dt.date.today()
    years = range(2025, today.year + 1) if full else [today.year]
    mid = midc_daily(years)
    fx = fx_daily("2024-12-01" if full else str(today - dt.timedelta(days=40)))
    new = pd.DataFrame({"midc_usd": mid, "usdcad": fx})
    new.index = pd.to_datetime(new.index)
    if old is not None:
        old = old.set_index("date")[["midc_usd", "usdcad"]]
        new = new.combine_first(old)
    new = new.sort_index()
    new.index.name = "date"
    new.reset_index().to_csv(PRICES, index=False, float_format="%.4f")
    return new


# ---------------------------------------------------------------- aggregation
def compute(h, prices):
    df = h.copy()
    df["local"] = df.index.tz_convert(TZ)
    df["date"] = pd.to_datetime(df["local"].dt.date)
    last_midc = prices["midc_usd"].last_valid_index()
    p = prices.reindex(pd.date_range(prices.index.min(), max(prices.index.max(), df["date"].max()))).ffill()
    df["midc_usd"] = df["date"].map(p["midc_usd"])
    df["usdcad"] = df["date"].map(p["usdcad"])
    df["us_price"] = df["midc_usd"] * df["usdcad"]
    df["us_price_est"] = df["date"] > last_midc
    # pool price: carry forward at most 3 hours for the not-yet-settled current hour
    df["ab_price_est"] = df["pool_cad"].isna()
    df["ab_price"] = df["pool_cad"].ffill(limit=3)
    for k in ("us", "ab"):
        mw, pr = df[f"{k}_mw"].astype(float), df[f"{k}_price"]
        ok = mw.notna() & pr.notna()
        df[f"{k}_ok"] = ok
        df[f"{k}_exp_mwh"] = mw.clip(lower=0).where(ok, 0)
        df[f"{k}_imp_mwh"] = (-mw).clip(lower=0).where(ok, 0)
        df[f"{k}_exp_cad"] = df[f"{k}_exp_mwh"] * pr.fillna(0)
        df[f"{k}_imp_cad"] = df[f"{k}_imp_mwh"] * pr.fillna(0)
        est_src = ["aeso", "bch5"] if k == "us" else ["aeso"]
        df[f"{k}_est"] = ok & (df[f"{k}_price_est"] | df[f"{k}_src"].isin(est_src))
        df[f"{k}_prov"] = ok & (df[f"{k}_src"] == "bch5")
    return df, last_midc


def summarize(sub):
    out = {}
    for k in ("us", "ab"):
        e, i = sub[f"{k}_exp_mwh"].sum(), sub[f"{k}_imp_mwh"].sum()
        ec, ic = sub[f"{k}_exp_cad"].sum(), sub[f"{k}_imp_cad"].sum()
        out[k] = {"exp_mwh": e, "imp_mwh": i, "net_mwh": e - i, "exp_cad": ec, "imp_cad": ic, "net_cad": ec - ic,
                  "hours": int(sub[f"{k}_ok"].sum()), "est_hours": int(sub[f"{k}_est"].sum()),
                  "prov_hours": int(sub[f"{k}_prov"].sum())}
    t = {f: out["us"][f] + out["ab"][f] for f in ("exp_mwh", "imp_mwh", "net_mwh", "exp_cad", "imp_cad", "net_cad")}
    out["total"] = t
    for v in out.values():
        v["avg_exp_price"] = v["exp_cad"] / v["exp_mwh"] if v["exp_mwh"] else None
        v["avg_imp_price"] = v["imp_cad"] / v["imp_mwh"] if v["imp_mwh"] else None
        for f in list(v):
            if isinstance(v[f], float):
                v[f] = round(v[f], 2) if "price" in f else round(v[f])
    hrs = len(sub)
    out["expected_hours"] = hrs
    return out


def build_dashboard(df, last_midc, live):
    now = pd.Timestamp.now(tz=TZ)
    ms = now.normalize().replace(day=1)
    pms = (ms - pd.Timedelta(days=1)).replace(day=1)
    ys = now.normalize().replace(month=1, day=1)
    pys = ys.replace(year=ys.year - 1)
    loc = df["local"]
    # last hour that has data for both ties (common "data through")
    has = df[df["us_ok"] | df["ab_ok"]]
    periods = {}
    defs = [("mtd", f"{now:%B %Y} to date", ms, now), ("prev_month", f"{pms:%B %Y}", pms, ms),
            ("ytd", f"{now.year} to date", ys, now), ("prev_year", f"{pys.year}", pys, ys)]
    for key, label, a, b in defs:
        sub = df[(loc >= a) & (loc < b)]
        s = summarize(sub)
        s["label"] = label
        s["start"] = a.isoformat()
        s["end"] = b.isoformat()
        for k in ("us", "ab"):
            ok = sub[sub[f"{k}_ok"]]
            s[k]["through"] = ok["local"].max().isoformat() if len(ok) else None
        periods[key] = s
    # YTD comparison: same span last year
    last = has["local"].max()
    ly = df[(loc >= pys) & (loc <= last - pd.DateOffset(years=1))]
    periods["ytd"]["same_period_last_year"] = summarize(ly)["total"]
    # monthly
    df["month"] = loc.dt.strftime("%Y-%m")
    monthly = []
    for m, g in df.groupby("month"):
        s = summarize(g)
        monthly.append({"month": m, "us_net_cad": s["us"]["net_cad"], "ab_net_cad": s["ab"]["net_cad"],
                        "us_net_mwh": s["us"]["net_mwh"], "ab_net_mwh": s["ab"]["net_mwh"],
                        "exp_mwh": s["total"]["exp_mwh"], "imp_mwh": s["total"]["imp_mwh"],
                        "est": bool(s["us"]["est_hours"] or s["ab"]["est_hours"])})
    # daily, last 60 days
    d60 = df[loc >= now.normalize() - pd.Timedelta(days=60)]
    daily = []
    for d, g in d60.groupby(g_ := d60["local"].dt.strftime("%Y-%m-%d")):
        s = summarize(g)
        daily.append({"date": d, "us_net_mwh": s["us"]["net_mwh"], "ab_net_mwh": s["ab"]["net_mwh"],
                      "us_net_cad": s["us"]["net_cad"], "ab_net_cad": s["ab"]["net_cad"],
                      "midc_cad": round(float(g["us_price"].mean()), 2) if g["us_price"].notna().any() else None,
                      "pool_cad": round(float(g["pool_cad"].mean()), 2) if g["pool_cad"].notna().any() else None})
    srcs = {}
    for k in ("us", "ab"):
        for src, g in df[df[f"{k}_ok"]].groupby(f"{k}_src"):
            srcs[f"{k}_{src}"] = {"from": g["local"].min().isoformat(), "to": g["local"].max().isoformat(), "hours": len(g)}
    return {"generated": now.isoformat(timespec="minutes"), "midc_last_delivery": str(pd.Timestamp(last_midc).date()),
            "us5_bias": META_BIAS.get("v"),
            "periods": periods, "monthly": monthly, "daily": daily, "sources": srcs, "live": live}


# ---------------------------------------------------------------- main
def main():
    full = "--backfill" in sys.argv
    check_tz()
    os.makedirs(D, exist_ok=True)
    meta = json.load(open(META)) if os.path.exists(META) else {}
    h = pd.DataFrame(columns=["us_mw", "us_src", "ab_mw", "ab_src", "pool_cad"], index=pd.DatetimeIndex([], tz="UTC")) if full else load_hourly()
    now_utc = pd.Timestamp.now(tz="UTC").floor("h")
    since = START.tz_convert("UTC") if full else now_utc - pd.Timedelta(days=10)
    today = dt.date.today()

    # 1. BC Hydro official hourly XLS (check at most every 6 h; download only if changed)
    if full or meta.get("xls_checked", "") < (now_utc - pd.Timedelta(hours=6)).isoformat():
        try:
            links = bch_xls_links()
            for y in ([2025, today.year] if full else sorted({today.year, (today - dt.timedelta(days=45)).year})):
                if y not in links:
                    continue
                raw = get(links[y])
                sig = f"{len(raw)}"
                if not full and meta.get(f"xls_{y}") == sig:
                    continue
                x = parse_bch_xls(raw)
                log(f"BC Hydro XLS {y}: {len(x)} h to {x.index.max()}")
                h = merge_flow(h, x["us"], "us", "bch")
                h = merge_flow(h, x["ab"], "ab", "bch")
                meta[f"xls_{y}"] = sig
            meta["xls_checked"] = now_utc.isoformat()
        except Exception as e:
            log("BC Hydro XLS failed:", e)

    # 2. EIA US tie (only after last official hour)
    try:
        bch_end = h.index[h["us_src"] == "bch"].max() if (h["us_src"] == "bch").any() else since
        e = eia_us(max(since, bch_end - pd.Timedelta(hours=1)))
        h = merge_flow(h, e, "us", "eia")
    except Exception as ex:
        log("EIA failed:", ex)

    # 3. BC Hydro 5-minute page (rolling 7 days, both ties)
    live = {}
    try:
        m5, latest = bch_5min()
        # The real-time page's BC-US figure reads lower than the official hourly/EIA series; calibrate it
        # with the median offset over overlapping hours and treat those hours as estimates.
        ov = h.loc[h.index.intersection(m5.index)]
        ov = ov[ov["us_src"].isin(["eia", "bch"])]
        bias = float((ov["us_mw"].astype(float) - m5.loc[ov.index, "us"]).median()) if len(ov) >= 24 else meta.get("us5_bias", 0.0)
        meta["us5_bias"] = round(bias, 1)
        log(f"  BC-US 5-min bias vs EIA: {bias:+.0f} MW over {len(ov)} h")
        h = merge_flow(h, m5["us"] + bias, "us", "bch5")
        h = merge_flow(h, m5["ab"], "ab", "bch5")
        live["bch"] = latest
    except Exception as ex:
        log("BC Hydro 5-min failed:", ex)

    # 4. AESO: pool price, AB-tie gap fill, live snapshot
    if AESO_KEY:
        try:
            pp = aeso_pool(since.tz_convert(TZ).date(), today)
            h = h.reindex(h.index.union(pp.index))
            h.loc[pp.index, "pool_cad"] = pp.values
            live["pool_price"] = {"t_utc": pp.index.max().isoformat(), "cad_mwh": float(pp.iloc[-1])}
        except Exception as ex:
            log("AESO pool price failed:", ex)
        try:
            span = h[(h.index >= since) & (h.index < now_utc - pd.Timedelta(days=6))]
            gap_days = sorted({t.tz_convert("America/Edmonton").date() for t in span.index[span["ab_src"].isna()]})
            if full:  # also any hours not yet in the frame between official data end and 5-min start
                b5 = h.index[h["ab_src"] == "bch5"]
                idx = pd.date_range(since, (b5.min() if len(b5) else now_utc) - pd.Timedelta(hours=1), freq="h")
                missing = idx.difference(h.index[h["ab_src"].notna()])
                gap_days = sorted({t.tz_convert("America/Edmonton").date() for t in missing})
            for d in gap_days[:40]:
                s = aeso_bc_sched(d)
                if s.empty:
                    log(f"  AESO metered {d}: not available"); continue
                h = merge_flow(h, s, "ab", "aeso")
        except Exception as ex:
            log("AESO gap fill failed:", ex)
        try:
            live["aeso"] = aeso_snapshot()
        except Exception as ex:
            log("AESO CSD failed:", ex)

    h = h[h.index >= START.tz_convert("UTC")].sort_index()
    h.index.name = "t_utc"
    for c in ("us_mw", "ab_mw", "pool_cad"):
        h[c] = pd.to_numeric(h[c], errors="coerce")
    h.to_csv(HOURLY, date_format="%Y-%m-%dT%H:%MZ", float_format="%.2f")
    prices = update_prices(full or not os.path.exists(PRICES))
    META_BIAS["v"] = meta.get("us5_bias")
    df, last_midc = compute(h, prices)
    dash = build_dashboard(df, last_midc, live)
    json.dump(dash, open(f"{D}/dashboard.json", "w"), indent=1, default=str)
    json.dump(meta, open(META, "w"), indent=1)
    for k, p in dash["periods"].items():
        t = p["total"]
        log(f"{p['label']:>18}: net {t['net_mwh']:>12,} MWh  net C${t['net_cad']:>14,}  (US {p['us']['net_cad']:,} / AB {p['ab']['net_cad']:,})")


if __name__ == "__main__":
    main()
