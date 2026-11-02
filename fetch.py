"""BC electricity trade dashboard: hourly BC-US and BC-AB intertie flows with estimated wholesale value.

Flows (MW, + = BC exporting), best source wins per hour:
  1. BC Hydro hourly tie-line data (official XLS, published ~monthly)        src "bch"
  2. EIA Hourly Electric Grid Monitor, BPAT->BCHA interchange (US tie only)  src "eia"
  3. BC Hydro 5-minute actual flow page (rolling 7 days), averaged hourly    src "bch5"
  4. AESO settled import/export volumes on the BC path (AB tie, gap fill)   src "aeso"  -> ESTIMATE
BC load: BC Hydro balancing-authority hourly Control Area Load (current-month + yearly XLS), cached in
  load_hourly.csv; generation derived as load + net tie exports; StatCan 25-10-0015 monthly generation as a check.
Prices: AESO hourly pool price (C$) for BC-AB; EIA/ICE Mid-C day-ahead PEAK price (US$) for BC-US, with
off-peak hours (WECC/NERC: HE23-HE6 Mon-Sat, all Sunday, NERC holidays; US Pacific prevailing time) priced at
OFFPEAK_FACTOR x peak (default 0.8, an assumption), converted with Bank of Canada daily USD/CAD.

Time zones: BC has been on permanent UTC-7 since Nov 2026 (BC Reg. 20/2026). BC-local grouping, display and the
BC Hydro files use America/Vancouver, which requires tzdata >= 2026 (permanent UTC-7 from 2026-11-01; historical
DST before that). The run aborts if the tz database is older (see check_tz). Mid-C delivery days and the
peak/off-peak blocks use America/Los_Angeles (US Pacific prevailing time), which differs from BC Nov-Mar.
Dollar values are market-price ESTIMATES (MWh x hub price), not BC Hydro/Powerex settlements.

Usage: python fetch.py              # incremental refresh, run every 15 min (trailing ~10 days)
       python fetch.py --backfill   # one-time rebuild from 2025-01-01
       python fetch.py --load-history # one-time: annual load summaries before 2025 -> load_annual.csv (records)
       python fetch.py --seed-audit # one-time: fill source_compare.csv with EIA + BC Hydro XLS from 2025-01-01
"""
import os, sys, io, re, json, datetime as dt, urllib.request, urllib.parse
import pandas as pd

EIA_KEY = os.environ.get("EIA_API_KEY", "DEMO_KEY")
AESO_KEY = os.environ.get("AESO_API_KEY")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36 bc-powerflow"}
TZ = "America/Vancouver"      # BC local (permanent UTC-7 from 2026-11-01 with tzdata >= 2026)
US_TZ = "America/Los_Angeles"  # Mid-C delivery days and WECC peak/off-peak blocks (US Pacific prevailing time)
START = pd.Timestamp("2025-01-01", tz=TZ)
D = "docs"
HOURLY, PRICES, META = f"{D}/hourly.csv", f"{D}/daily_prices.csv", f"{D}/meta.json"
AUDIT = f"{D}/source_compare.csv"  # every source's MW per hour (not only the winning one), for QA
AUDIT_COLS = ["us_bch", "us_eia", "us_bch5_raw", "ab_bch", "ab_bch5", "ab_aeso"]
BCH = "https://www.bchydro.com"
META_BIAS = {}
RANK = {"bch": 3, "eia": 2, "bch5": 1, "aeso": 0}
# Base-case assumption: Mid-C off-peak hours (NERC: HE23-HE6 Mon-Sat, all Sunday and NERC holidays, US Pacific
# prevailing time) are priced at this multiple of the Mid-C day-ahead PEAK price. The "peak_only" alternative
# prices every hour at the peak index (factor 1.0).
OFFPEAK_FACTOR = float(os.environ.get("OFFPEAK_FACTOR", "0.8"))


def check_tz():
    """BC moved to permanent UTC-7 in Nov 2026; needs tzdata >= 2026 (the workflow pins the tzdata package and
    sets PYTHONTZPATH="" so the system zoneinfo is not used). Abort rather than publish hour-shifted data."""
    o = lambda tz, t: pd.Timestamp(t).tz_localize(tz).utcoffset()
    checks = {(TZ, "2026-12-15 12:00"): -7, (TZ, "2025-12-15 12:00"): -8, (TZ, "2025-07-01 12:00"): -7,
              (US_TZ, "2026-12-15 12:00"): -8, (US_TZ, "2027-07-01 12:00"): -7}
    bad = {k: str(o(*k)) for k, v in checks.items() if o(*k) != pd.Timedelta(hours=v)}
    if bad:
        log(f"ERROR: tz database out of date (need tzdata >= 2026 with BC permanent UTC-7): {bad}")
        sys.exit(2)
    log("tz check ok: America/Vancouver UTC-7 from 2026-11-01, America/Los_Angeles keeps DST")


def nerc_holidays(years):
    """NERC off-peak holidays. A holiday falling on Sunday is observed on Monday; Saturday holidays are not moved."""
    out = set()
    for y in years:
        days = [pd.Timestamp(y, 1, 1), pd.Timestamp(y, 7, 4), pd.Timestamp(y, 12, 25),
                pd.Timestamp(y, 5, 31) - pd.Timedelta(days=pd.Timestamp(y, 5, 31).dayofweek),  # Memorial: last Mon May
                pd.Timestamp(y, 9, 1) + pd.Timedelta(days=(0 - pd.Timestamp(y, 9, 1).dayofweek) % 7),  # Labor: 1st Mon Sep
                pd.Timestamp(y, 11, 1) + pd.Timedelta(days=(3 - pd.Timestamp(y, 11, 1).dayofweek) % 7 + 21)]  # 4th Thu Nov
        for d in days:
            out.add((d + pd.Timedelta(days=1) if d.dayofweek == 6 else d).date())
    return out


# ---------------------------------------------------------------- source audit
def load_audit():
    if os.path.exists(AUDIT):
        a = pd.read_csv(AUDIT)
        a.index = pd.to_datetime(a.pop("t_utc"), utc=True)
        return a.reindex(columns=AUDIT_COLS)
    return pd.DataFrame(columns=AUDIT_COLS, index=pd.DatetimeIndex([], tz="UTC"), dtype=float)


def audit_put(a, s, col):
    s = s.dropna().astype(float)
    if s.empty:
        return a
    a = a.reindex(a.index.union(s.index))
    a.loc[s.index, col] = s.values
    return a


def compare(a, x, y, min_n=24):
    """Bias and error of source x vs reference y (MW) over hours where both exist."""
    both = a[[x, y]].dropna()
    if len(both) < min_n:
        return {"hours": int(len(both))}
    d = both[x] - both[y]
    return {"hours": int(len(d)), "from": both.index.min().tz_convert(TZ).isoformat(),
            "to": both.index.max().tz_convert(TZ).isoformat(),
            "mean_bias_mw": round(float(d.mean()), 1), "median_bias_mw": round(float(d.median()), 1),
            "mae_mw": round(float(d.abs().mean()), 1), "corr": round(float(both[x].corr(both[y])), 4)}


def source_qa(a):
    a = a.copy()
    return {"note": f"x minus reference, MW, over overlapping hours; per-hour values for every source in {AUDIT.split('/')[-1]}. "
                    "bch5_raw is the uncorrected real-time page value.",
            "us_eia_vs_bch": compare(a, "us_eia", "us_bch"),
            "us_bch5_raw_vs_bch": compare(a, "us_bch5_raw", "us_bch"),
            "us_bch5_raw_vs_eia": compare(a, "us_bch5_raw", "us_eia"),
            "ab_bch5_vs_bch": compare(a, "ab_bch5", "ab_bch"),
            "ab_aeso_vs_bch": compare(a, "ab_aeso", "ab_bch"),
            "ab_aeso_vs_bch5": compare(a, "ab_aeso", "ab_bch5")}


def get(url, headers=None, timeout=90):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    return urllib.request.urlopen(req, timeout=timeout).read()


def head_size(url):
    """Content-Length via HEAD (cheap change check for big files); None if unavailable."""
    try:
        req = urllib.request.Request(url, headers=UA, method="HEAD")
        n = urllib.request.urlopen(req, timeout=30).headers.get("Content-Length")
        return n if n and int(n) > 0 else None
    except Exception:
        return None


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


# ---------------------------------------------------------------- BC load (BC Hydro balancing authority)
# BC Hydro is the Balancing Authority (BCHA) for British Columbia. It publishes hourly "gross telemetered" Control
# Area Load (MW, hour-ending, BC time): a current-month file updated each business day for the previous day(s)
# and yearly files (current year updated ~monthly). There is no public 5-minute load or generation feed, and EIA's
# grid monitor has no BCHA demand/generation (Canadian BAs don't file EIA-930; only the BPAT-BCHA interchange).
# Generation inside the BA is derived: gen = load + net exports on the US and AB tie lines.
LOAD, LOAD_HIST, STATCAN = f"{D}/load_hourly.csv", f"{D}/load_annual.csv", f"{D}/statcan_bc_gen.csv"
BA_DIR = "/content/dam/BCHydro/customer-portal/documents/corporate/suppliers/transmission-system/balancing_authority_load_data"
BA_PAGES = [f"{BCH}/energy-in-bc/operations/transmission/transmission-system/balancing-authority-load-data.html",
            f"{BCH}/energy-in-bc/operations/transmission/transmission-system/balancing-authority-load-data/historical-transmission-data.html"]
BA_FALLBACK = {"current": f"{BCH}{BA_DIR}/CurrentHourlyBALoad.xls",
               2026: f"{BCH}{BA_DIR}/BalancingAuthorityLoad%202026.xls",
               2025: f"{BCH}{BA_DIR}/Historical%20Transmission%20Data/BalancingAuthorityLoad%202025.xls"}


def head_sig(url):
    """Content-Length + Last-Modified via HEAD (cheap change check); None if unavailable."""
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, headers=UA, method="HEAD"), timeout=30)
        n, lm = r.headers.get("Content-Length"), r.headers.get("Last-Modified")
        return f"{n}|{lm}" if n else None
    except Exception:
        return None


def ba_load_links():
    links = dict(BA_FALLBACK)
    for page in BA_PAGES:
        try:
            html = get(page).decode("utf8", "ignore")
        except Exception as e:
            log("  BA load page failed:", page, e); continue
        for href in re.findall(r'href="([^"]*balancing_authority_load_data/[^"]*\.xls)"', html):
            path = urllib.parse.unquote(href)
            name = path.rsplit("/", 1)[-1]
            url = BCH + urllib.parse.quote(path)
            m = re.fullmatch(r"BalancingAuthorityLoad ?(\d{4})\.xls|(?:jandec)?(\d{4})controlareaload\.xls", name, re.I)
            if m:
                links[int(m[1] or m[2])] = url
            elif name == "CurrentHourlyBALoad.xls":
                links["current"] = url
    return links


def parse_load_xls(raw):
    """BC Hydro Hourly Control Area Load report -> Series of MW indexed by UTC hour-beginning. Rows are hour-ending
    in BC local time; each day's rows are laid out consecutively from local midnight (23 on spring-forward days,
    25 with an 'HE 2*' row on fall-back days), as for the tie-line files."""
    t = pd.read_excel(io.BytesIO(raw), header=None)
    hdr = t.index[t.iloc[:, 2].astype(str).str.contains("Control Area Load", na=False)]
    t = t.loc[(hdr[0] + 1 if len(hdr) else 0):, :2]
    t.columns = ["Date", "HE", "L"]
    t["Date"] = pd.to_datetime(t["Date"], format="mixed", errors="coerce")
    t["L"] = pd.to_numeric(t["L"], errors="coerce")
    t = t.dropna(subset=["Date"])
    out = []
    for day, g in t.groupby("Date", sort=True):
        t0 = pd.Timestamp(day).tz_localize(TZ).tz_convert("UTC")
        out.append(pd.Series(g["L"].values, index=[t0 + pd.Timedelta(hours=i) for i in range(len(g))]))
    s = pd.concat(out).dropna() if out else pd.Series(dtype=float)
    return s[(s > 1000) & (s < 25000)]  # drop blanks / obvious placeholders


def load_series(path=LOAD):
    if os.path.exists(path):
        x = pd.read_csv(path)
        return pd.Series(x["load_mw"].values, index=pd.to_datetime(x["t_utc"], utc=True), dtype=float)
    return pd.Series(dtype=float, index=pd.DatetimeIndex([], tz="UTC"))


def update_load(meta, full, now_utc):
    """Incremental: HEAD-check the small current-month file at most every 55 min and the yearly file(s) at most
    every 6 h; download only when size/Last-Modified changed. Cached in load_hourly.csv."""
    full = full or not os.path.exists(LOAD)  # first run: fetch all yearly files from START
    lo = pd.Series(dtype=float, index=pd.DatetimeIndex([], tz="UTC")) if full else load_series()
    due_cur = full or meta.get("load_cur_checked", "") < (now_utc - pd.Timedelta(minutes=55)).isoformat()
    due_year = full or meta.get("load_year_checked", "") < (now_utc - pd.Timedelta(hours=6)).isoformat()
    if not (due_cur or due_year):
        return lo
    links = ba_load_links() if (due_year or full) else dict(BA_FALLBACK)
    today = dt.date.today()
    years = range(START.year, today.year + 1) if full else sorted({today.year, (today - dt.timedelta(days=45)).year})
    todo = ([y for y in years if y in links] if due_year else []) + (["current"] if due_cur else [])
    for k in todo:
        try:
            sig = head_sig(links[k])
            if not full and sig is not None and meta.get(f"load_{k}") == sig:
                continue
            s = parse_load_xls(get(links[k]))
            log(f"  BC load {k}: {len(s)} h to {s.index.max() if len(s) else None}")
            lo = s.combine_first(lo)
            if sig:
                meta[f"load_{k}"] = sig
        except Exception as e:
            log(f"  BC load {k} failed:", e)
    if due_cur:
        meta["load_cur_checked"] = now_utc.isoformat()
    if due_year:
        meta["load_year_checked"] = now_utc.isoformat()
    lo = lo[lo.index >= START.tz_convert("UTC")].sort_index()
    pd.DataFrame({"t_utc": lo.index.strftime("%Y-%m-%dT%H:%MZ"), "load_mw": lo.values}).to_csv(LOAD, index=False, float_format="%.0f")
    return lo


def load_history():
    """One-time (--load-history): annual BC BA load summaries from BC Hydro's yearly files before START, for record context."""
    links, rows = ba_load_links(), []
    for y in sorted(k for k in links if isinstance(k, int) and k < START.year):
        try:
            s = parse_load_xls(get(links[y]))
        except Exception as e:
            log(f"  load history {y} failed:", e); continue
        loc = s.index.tz_convert(TZ)
        s = s[loc.year == y]
        if s.empty:
            continue
        rows.append({"year": y, "load_gwh": round(s.sum() / 1e3), "avg_mw": round(s.mean()), "peak_mw": int(s.max()),
                     "peak_t": s.idxmax().tz_convert(TZ).isoformat(), "min_mw": int(s.min()), "hours": len(s)})
        log(f"  load history {y}: peak {s.max():.0f} MW")
    pd.DataFrame(rows).to_csv(LOAD_HIST, index=False)


def update_statcan(meta, now_utc):
    """Statistics Canada table 25-10-0015-01 (monthly electricity generation, BC, all producers), ~1 MB zip, checked
    at most daily. Used only as an independent monthly cross-check of the derived generation."""
    if os.path.exists(STATCAN) and meta.get("statcan_checked", "") >= (now_utc - pd.Timedelta(hours=24)).isoformat():
        return pd.read_csv(STATCAN)
    try:
        import zipfile
        z = zipfile.ZipFile(io.BytesIO(get("https://www150.statcan.gc.ca/n1/tbl/csv/25100015-eng.zip", timeout=120)))
        t = pd.read_csv(z.open("25100015.csv"), usecols=["REF_DATE", "GEO", "Class of electricity producer",
                                                         "Type of electricity generation", "VALUE"], low_memory=False)
        t = t[(t["GEO"] == "British Columbia") & (t["Class of electricity producer"] == "Total all classes of electricity producer")
              & (t["REF_DATE"] >= "2024-01")]
        typ = t["Type of electricity generation"]
        tot = t[typ == "Total all types of electricity generation"].set_index("REF_DATE")["VALUE"]
        hyd = t[typ == "Hydraulic turbine"].set_index("REF_DATE")["VALUE"]
        out = pd.DataFrame({"gen_mwh": tot, "hydro_mwh": hyd}).dropna(how="all")
        out.index.name = "month"
        out.reset_index().to_csv(STATCAN, index=False, float_format="%.0f")
        meta["statcan_checked"] = now_utc.isoformat()
        log(f"  StatCan BC generation to {out.index.max()}")
        return out.reset_index()
    except Exception as e:
        log("  StatCan failed:", e)
        return pd.read_csv(STATCAN) if os.path.exists(STATCAN) else pd.DataFrame(columns=["month", "gen_mwh", "hydro_mwh"])


def _t(ts):
    return ts.tz_convert(TZ).isoformat() if ts is not None and not pd.isna(ts) else None


def load_stats(sub):
    """Load/generation metrics for a slice of compute() output. All values hourly averages (MW) from BC Hydro's
    hourly files; peaks are the highest HOURLY average, not instantaneous. Times are hour-beginning, BC time.
    Shares use hours with load and both tie flows."""
    L = sub["load_mw"].dropna()
    net = (sub["us_mw"].astype(float) + sub["ab_mw"].astype(float)).dropna()  # + = BC net export
    out = {"load_hours": int(len(L))}
    if len(net):
        out.update({"peak_net_import_mw": max(0, int(round(-net.min()))), "peak_net_import_t": _t(net.idxmin()) if net.min() < 0 else None,
                    "peak_net_export_mw": int(round(net.max())), "peak_net_export_t": _t(net.idxmax()),
                    "net_import_hours_pct": round(float((net < 0).mean() * 100), 1)})
    if L.empty:
        return out
    gen = (sub["load_mw"] + net).dropna()
    nb = net.reindex(gen.index)
    out.update({"load_from": _t(L.index.min()), "load_through": _t(L.index.max() + pd.Timedelta(hours=1)),
                "load_mwh": round(float(L.sum())), "avg_load_mw": round(float(L.mean())),
                "peak_load_mw": int(L.max()), "peak_load_t": _t(L.idxmax()),
                "min_load_mw": int(L.min()), "min_load_t": _t(L.idxmin()),
                "load_factor": round(float(L.mean() / L.max()), 3)})
    if len(gen):
        lb = sub["load_mw"].reindex(gen.index)
        out.update({"gen_hours": int(len(gen)), "gen_mwh": round(float(gen.sum())), "avg_gen_mw": round(float(gen.mean())),
                    "peak_gen_mw": int(round(gen.max())), "peak_gen_t": _t(gen.idxmax()),
                    "net_imp_mwh": round(float((-nb).clip(lower=0).sum())), "net_exp_mwh": round(float(nb.clip(lower=0).sum())),
                    "import_share_of_load_pct": round(float((-nb).clip(lower=0).sum() / lb.sum() * 100), 1),
                    "export_share_of_gen_pct": round(float(nb.clip(lower=0).sum() / gen.sum() * 100), 1),
                    "gen_to_load_pct": round(float(gen.sum() / lb.sum() * 100), 1)})
    return out


def load_context(df, statcan):
    """Records over the dataset, annual history, and the StatCan monthly cross-check of derived generation."""
    st = load_stats(df)
    rec = {k: st.get(k) for k in ("peak_load_mw", "peak_load_t", "min_load_mw", "min_load_t", "peak_gen_mw", "peak_gen_t",
                                  "peak_net_import_mw", "peak_net_import_t", "peak_net_export_mw", "peak_net_export_t",
                                  "load_from", "load_through")}
    annual = pd.read_csv(LOAD_HIST).to_dict("records") if os.path.exists(LOAD_HIST) else []
    L = df["load_mw"].dropna()
    for y, s in L.groupby(L.index.tz_convert(TZ).year):
        annual.append({"year": int(y), "load_gwh": round(s.sum() / 1e3), "avg_mw": round(s.mean()), "peak_mw": int(s.max()),
                       "peak_t": _t(s.idxmax()), "min_mw": int(s.min()), "hours": int(len(s)),
                       "partial": bool(len(s) < 8700)})
    full_years = [a for a in annual if not a.get("partial")] or annual
    best = max(annual, key=lambda a: a["peak_mw"]) if annual else None
    # StatCan cross-check: derived monthly generation vs StatCan BC total generation (complete months only)
    chk = []
    sc = statcan.set_index("month") if len(statcan) else None
    loc = df["local"]
    for m, g in df.groupby(loc.dt.strftime("%Y-%m")):
        gen = (g["load_mw"] + g["us_mw"].astype(float) + g["ab_mw"].astype(float)).dropna()
        a = pd.Timestamp(m + "-01").tz_localize(TZ)
        cal = int(((a + pd.DateOffset(months=1)) - a) / pd.Timedelta(hours=1))
        if sc is None or m not in sc.index or len(gen) < cal - 2 or pd.isna(sc.loc[m, "gen_mwh"]):
            continue
        s = sc.loc[m]
        chk.append({"month": m, "derived_gen_mwh": round(float(gen.sum())), "statcan_gen_mwh": round(float(s["gen_mwh"])),
                    "diff_pct": round(float(gen.sum() / s["gen_mwh"] * 100 - 100), 1),
                    "statcan_hydro_pct": round(float(s["hydro_mwh"] / s["gen_mwh"] * 100), 1) if not pd.isna(s["hydro_mwh"]) else None})
    d = [c["diff_pct"] for c in chk]
    return {"definition": "BC Hydro Balancing Authority (BCHA) gross telemetered Control Area Load: hourly average MW for "
                          "the BC Hydro balancing authority area, which covers British Columbia's interconnected grid. "
                          "Generation is derived as load + net exports on the BC-US and BC-AB tie lines (the BA's interchange), "
                          "so load = generation + net imports holds by construction.",
            "records": rec, "record_since": min([a["year"] for a in annual], default=None),
            "record_peak": best, "annual": sorted(annual, key=lambda a: a["year"]),
            "statcan_check": {"months": chk, "mean_diff_pct": round(sum(d) / len(d), 1) if d else None,
                              "min_diff_pct": min(d, default=None), "max_diff_pct": max(d, default=None),
                              "note": "Derived generation (BCHA load + net tie exports) vs Statistics Canada table 25-10-0015-01, "
                                      "BC total generation, all producers. StatCan also counts generation outside the telemetered "
                                      "BA load (e.g. off-grid and some industrial self-generation), so derived is expected to be a few % lower.",
                              "source": "https://www150.statcan.gc.ca/t1/tbl1/en/tv.action?pid=2510001501"},
            "instantaneous": "Not published: BC Hydro has no public 5-minute or instantaneous load/generation feed (the 5-minute "
                             "real-time page has tie-line flows only), so peaks here are highest hourly averages."}


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


def load_prices():
    p = pd.read_csv(PRICES, parse_dates=["date"]).set_index("date")[["midc_usd", "usdcad"]]
    return p.sort_index()


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
    df["local"] = df.index.tz_convert(TZ)        # BC time: grouping and display
    us_loc = df.index.tz_convert(US_TZ)           # US Pacific prevailing time: Mid-C delivery day, WECC blocks
    df["date"] = pd.to_datetime(pd.Series(us_loc.date, index=df.index))
    last_midc = prices["midc_usd"].last_valid_index()
    p = prices.reindex(pd.date_range(prices.index.min(), max(prices.index.max(), df["date"].max()))).ffill()
    df["midc_usd"] = df["date"].map(p["midc_usd"])
    df["usdcad"] = df["date"].map(p["usdcad"])
    df["us_price_pk"] = df["midc_usd"] * df["usdcad"]  # peak index in every hour (alternative)
    df["us_price_est"] = df["date"] > last_midc
    hb = pd.Series(us_loc.hour, index=df.index)  # hour beginning, US Pacific prevailing; HB22..HB5 = HE23..HE6
    hol = nerc_holidays(range(us_loc.year.min(), us_loc.year.max() + 1)) if len(df) else set()
    df["offpeak"] = ((hb >= 22) | (hb < 6) | pd.Series(us_loc.dayofweek == 6, index=df.index)
                     | pd.Series([d in hol for d in us_loc.date], index=df.index))
    df["us_price"] = df["us_price_pk"] * df["offpeak"].map({True: OFFPEAK_FACTOR, False: 1.0})  # base case
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
    df["ab_price_pk"] = df["ab_price"]  # AESO pool price is already hourly
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
    return out


def completeness(sub, a, b, now):
    """Calendar hours in [a, min(b, end of current hour)) versus hours with a valued flow, per tie. 'missing_to_through' counts
    gaps up to the tie's latest data hour (true holes); hours after that are just source lag."""
    end = min(b.ceil("h"), now.floor("h") + pd.Timedelta(hours=1))  # include the current (partial) hour
    cal = max(0, int((end - a) / pd.Timedelta(hours=1)))
    out = {"calendar_hours": cal}
    for k in ("us", "ab"):
        ok = sub[sub[f"{k}_ok"]]
        thr = ok.index.max() + pd.Timedelta(hours=1) if len(ok) else a
        to_thr = max(0, int((min(thr, end) - a) / pd.Timedelta(hours=1)))
        out[k] = {"valued_hours": int(len(ok)), "missing_to_through": max(0, to_thr - len(ok)),
                  "lag_hours": max(0, cal - to_thr)}
    return out


def _arb_month(sub, sfx):
    timing, e_mwh, i_mwh, e_cad, i_cad, per = 0.0, 0.0, 0.0, 0.0, 0.0, {"us": 0.0, "ab": 0.0}
    for k in ("us", "ab"):
        ok = sub[sub[f"{k}_ok"]]
        if ok.empty:
            continue
        mw, p = ok[f"{k}_mw"].astype(float), ok[f"{k}_price{sfx}"]
        per[k] = float((mw * (p - p.mean())).sum())
        e, i = mw.clip(lower=0), (-mw).clip(lower=0)
        e_mwh += e.sum(); i_mwh += i.sum(); e_cad += (e * p).sum(); i_cad += (i * p).sum()
    spread = (e_cad / e_mwh - i_cad / i_mwh) * min(e_mwh, i_mwh) if e_mwh and i_mwh else 0.0
    return per, spread, min(e_mwh, i_mwh), e_mwh, i_mwh, e_cad, i_cad


def arb_metrics(sub, sfx=""):
    """Decomposition lines of the net value (both are components of E*Pe - I*Pi, not additions, and they overlap),
    computed per calendar month and summed, so rolling totals equal the sum of their months. Price columns:
    <tie>_price<sfx>.
    timing_cad: sum of flow x (hourly price - that tie's simple average price for the month), i.e. value from
                WHEN within the month BC exported/imported, versus moving the same net energy flat.
    spread_cad: (flow-weighted avg export price - flow-weighted avg import price) x min(export, import MWh)
                for the month (both ties pooled), i.e. the margin on matched import/export volume. Since
                net = spread - (I - E) * Pi_avg when I > E (or + (E - I) * Pe_avg when E > I), it is part of net."""
    tot = {"us": 0.0, "ab": 0.0}
    spread = matched = e_mwh = i_mwh = e_cad = i_cad = 0.0
    if len(sub):
        for _, g in sub.groupby(sub["local"].dt.strftime("%Y-%m")):
            per, sp, mt, em, im, ec, ic = _arb_month(g, sfx)
            tot["us"] += per["us"]; tot["ab"] += per["ab"]
            spread += sp; matched += mt; e_mwh += em; i_mwh += im; e_cad += ec; i_cad += ic
    return {"timing_cad": round(tot["us"] + tot["ab"]), "timing_us_cad": round(tot["us"]), "timing_ab_cad": round(tot["ab"]),
            "spread_cad": round(spread), "matched_mwh": round(matched),
            "avg_exp_price": round(e_cad / e_mwh, 2) if e_mwh else None,
            "avg_imp_price": round(i_cad / i_mwh, 2) if i_mwh else None,
            "net_cad": round(e_cad - i_cad), "imp_cad": round(i_cad), "exp_cad": round(e_cad)}


def arb_block(sub):
    return {"base": arb_metrics(sub), "peak_only": arb_metrics(sub, "_pk")}


# BC Hydro published figures (static; fiscal years end Mar 31). Sources in REPORTED["sources"].
REPORTED = {
    "fy2026": {"label": "FY2026 (Apr 2025 - Mar 2026)", "net_import_gwh": 4344, "imp_gwh": 5957, "imp_cad_m": 305,
               "imp_price": 51, "exp_gwh": 1613, "exp_cad_m": 60, "exp_price": 37, "net_cost_cad_m": 245,
               "trade_revenue_cad_m": 1244},
    "fy2025": {"label": "FY2025 (Apr 2024 - Mar 2025)", "net_import_gwh": 8356, "imp_gwh": 9082, "imp_cad_m": 528,
               "imp_price": 58, "exp_gwh": 726, "exp_cad_m": 32, "exp_price": 44, "net_cost_cad_m": 496,
               "trade_revenue_cad_m": 1429},
    "trade_revenue_cy2025_cad_m": 1430,
    "trade_income_ranges": [{"span": "FY2021-FY2025", "low_cad_m": 386, "high_cad_m": 1051},
                            {"span": "FY2022-FY2026", "low_cad_m": 379, "high_cad_m": 1052}],
    "fy2026_trade_income_inferred_cad_m": 379,
    "fy2026_tida_addition_cad_m": 167,
    "sources": [
        {"label": "BC Hydro 2025/26 Annual Service Plan Report (net trade table p.136, Appendix B p.64, MD&A p.25-26)",
         "url": "https://bcbudget.gov.bc.ca/Annual_Reports/2025_2026/pdf/agency/bchydro.pdf"},
        {"label": "BC Hydro 2024/25 Annual Service Plan Report (Appendix B p.68)",
         "url": "https://bcbudget.gov.bc.ca/Annual_Reports/2024_2025/pdf/agency/bchydro.pdf"},
        {"label": "BC Hydro 2025/26 audited financial statements",
         "url": "https://www2.gov.bc.ca/assets/gov/british-columbians-our-governments/government-finances/public-accounts/2025-26/sup-e/bc-hydro-fs-2025-26.pdf"}],
}


def reported_context(df):
    loc = df["local"]
    a, b = pd.Timestamp("2025-04-01", tz=TZ), pd.Timestamp("2026-04-01", tz=TZ)
    sub = df[(loc >= a) & (loc < b)]
    t = summarize(sub)["total"]
    sens = arb_metrics(sub, "_pk")
    r = REPORTED["fy2026"]
    hub = {"imp_mwh": t["imp_mwh"], "imp_cad": t["imp_cad"], "exp_mwh": t["exp_mwh"], "exp_cad": t["exp_cad"],
           "net_cad": t["net_cad"], "avg_imp_price": t["avg_imp_price"], "avg_exp_price": t["avg_exp_price"],
           "peak_only_imp_cad": sens["imp_cad"], "peak_only_exp_cad": sens["exp_cad"], "peak_only_net_cad": sens["net_cad"]}
    cal = {"imp_price_ratio": float(round(r["imp_cad_m"] * 1e6 / r["imp_gwh"] / 1e3 / t["avg_imp_price"], 2)) if t["avg_imp_price"] else None,
           "exp_price_ratio": float(round(r["exp_cad_m"] * 1e6 / r["exp_gwh"] / 1e3 / t["avg_exp_price"], 2)) if t["avg_exp_price"] else None,
           "note": "Different measures, not like-for-like: tie-line flows include Powerex trading and other "
                   "non-purchase flows valued at hub prices; BC Hydro's reported trade is its domestic purchases and "
                   "sales at Powerex transfer prices. Ratios are descriptive only."}
    return {**REPORTED, "dashboard_fy2026": hub, "different_measures_fy2026": cal}


def build_dashboard(df, last_midc, live, qa=None, statcan=None):
    now = pd.Timestamp.now(tz=TZ)
    ms = now.normalize().replace(day=1)
    pms = (ms - pd.Timedelta(days=1)).replace(day=1)
    ys = now.normalize().replace(month=1, day=1)
    pys = ys.replace(year=ys.year - 1)
    loc = df["local"]
    # last hour that has data for both ties (common "data through")
    has = df[df["us_ok"] | df["ab_ok"]]
    periods, arb_periods = {}, {}
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
        s["completeness"] = completeness(sub, a, b, now)
        s["load"] = load_stats(sub)
        periods[key] = s
        arb_periods[key] = {"label": label, **arb_block(sub)}
    # YTD comparison: same span last year
    last = has["local"].max()
    ly = df[(loc >= pys) & (loc <= last - pd.DateOffset(years=1))]
    periods["ytd"]["same_period_last_year"] = summarize(ly)["total"]
    # monthly
    df["month"] = loc.dt.strftime("%Y-%m")
    monthly, arb_monthly = [], []
    for m, g in df.groupby("month"):
        s = summarize(g)
        ls = load_stats(g)
        m0 = pd.Timestamp(m + "-01").tz_localize(TZ)
        cal_h = int((min(m0 + pd.DateOffset(months=1), now.ceil("h")) - m0) / pd.Timedelta(hours=1))
        monthly.append({"month": m, "us_net_cad": s["us"]["net_cad"], "ab_net_cad": s["ab"]["net_cad"],
                        "load_mwh": ls.get("load_mwh"), "gen_mwh": ls.get("gen_mwh"), "peak_load_mw": ls.get("peak_load_mw"),
                        "avg_load_mw": ls.get("avg_load_mw"), "load_hours": ls["load_hours"],
                        "load_partial": bool(0 < ls["load_hours"] < cal_h - 1),
                        "us_net_mwh": s["us"]["net_mwh"], "ab_net_mwh": s["ab"]["net_mwh"],
                        "exp_mwh": s["total"]["exp_mwh"], "imp_mwh": s["total"]["imp_mwh"],
                        "est": bool(s["us"]["est_hours"] or s["ab"]["est_hours"])})
        ab_ = arb_block(g)
        arb_monthly.append({"month": m, "timing_cad": ab_["base"]["timing_cad"], "spread_cad": ab_["base"]["spread_cad"],
                            "timing_cad_peak_only": ab_["peak_only"]["timing_cad"],
                            "spread_cad_peak_only": ab_["peak_only"]["spread_cad"],
                            "net_cad_peak_only": ab_["peak_only"]["net_cad"]})
    # daily, last 60 days
    d60 = df[loc >= now.normalize() - pd.Timedelta(days=60)]
    daily = []
    for d, g in d60.groupby(g_ := d60["local"].dt.strftime("%Y-%m-%d")):
        s = summarize(g)
        daily.append({"date": d, "us_net_mwh": s["us"]["net_mwh"], "ab_net_mwh": s["ab"]["net_mwh"],
                      "us_net_cad": s["us"]["net_cad"], "ab_net_cad": s["ab"]["net_cad"],
                      "midc_cad": round(float(g["us_price_pk"].mean()), 2) if g["us_price_pk"].notna().any() else None,
                      "pool_cad": round(float(g["pool_cad"].mean()), 2) if g["pool_cad"].notna().any() else None})
    srcs = {}
    for k in ("us", "ab"):
        for src, g in df[df[f"{k}_ok"]].groupby(f"{k}_src"):
            srcs[f"{k}_{src}"] = {"from": g["local"].min().isoformat(), "to": g["local"].max().isoformat(), "hours": len(g)}
    return {"generated": now.isoformat(timespec="minutes"), "midc_last_delivery": str(pd.Timestamp(last_midc).date()),
            "us5_bias": META_BIAS.get("v"),
            "periods": periods, "monthly": monthly, "daily": daily, "sources": srcs, "live": live,
            "source_qa": qa or {},
            "valuation": {"us": f"Mid-C day-ahead peak price; off-peak hours priced at {OFFPEAK_FACTOR}x (assumption)",
                          "offpeak_factor": OFFPEAK_FACTOR,
                          "offpeak_hours": "US Pacific prevailing time: hour-ending 23-6 Mon-Sat, all Sunday, NERC holidays "
                                           "(New Year's, Memorial Day, Independence Day, Labor Day, Thanksgiving, Christmas; "
                                           "Sunday holidays observed Monday)",
                          "ab": "AESO hourly pool price"},
            "arbitrage_estimate": {
                "note": "Breakdown of the headline total, computed from the same hourly flows and hub prices. Both lines "
                        "are PART OF the total (not additional) and overlap each other; do not add them to the total or "
                        "to each other. Not Powerex's actual trading profit (published only as multi-year ranges).",
                "labels": {"timing_cad": "Of the total above: gain from better-timed trades",
                           "spread_cad": "Of the total above: margin on matched import/export volume",
                           "peak_only": "Alternative valuation with the Mid-C peak price in every hour"},
                "assumptions": {"offpeak_factor": OFFPEAK_FACTOR,
                                "offpeak_hours": "US Pacific prevailing hour-ending 23-6 Mon-Sat, all Sunday, NERC holidays",
                                "applies_to": "Mid-C (BC-US) price only; AESO pool price is already hourly"},
                "periods": arb_periods, "monthly": arb_monthly},
            "reported_context": reported_context(df),
            "load_context": load_context(df, statcan if statcan is not None else pd.DataFrame())}


def data_through(h, prices, live, load=None):
    load = load if load is not None else pd.Series(dtype=float)
    """Latest actual (not carried-forward) timestamp per source, so the page can show data freshness
    separately from the run time. Flow times are the END of the latest hour present (hour-beginning + 1 h)."""
    def last_hour(col, src):
        m = h[f"{col}_src"] == src
        m &= h[f"{col}_mw"].notna()
        return (h.index[m].max() + pd.Timedelta(hours=1)).isoformat() if m.any() else None
    pool = h.index[h["pool_cad"].notna()]
    mid, fx = prices["midc_usd"].last_valid_index(), prices["usdcad"].last_valid_index()
    return {
        "us_eia": {"label": "BC–US flows (EIA hourly)", "t": last_hour("us", "eia")},
        "us_bch": {"label": "BC–US flows (BC Hydro official)", "t": last_hour("us", "bch")},
        "ab_bch": {"label": "BC–Alberta flows (BC Hydro official)", "t": last_hour("ab", "bch")},
        "ab_aeso": {"label": "BC–Alberta flows (AESO settled schedules)", "t": last_hour("ab", "aeso")},
        "bch5": {"label": "BC Hydro 5-min real-time page", "t": (live.get("bch") or {}).get("t_utc")},
        "pool": {"label": "AESO pool price", "t": (pool.max() + pd.Timedelta(hours=1)).isoformat() if len(pool) else None},
        "load": {"label": "BC load (BC Hydro balancing authority, hourly)",
                 "t": (lambda L: (L.index.max() + pd.Timedelta(hours=1)).isoformat() if len(L) else None)(load.dropna()),
                 "note": "posted each business day for the previous day(s)"},
        "midc": {"label": "Mid-C peak price (delivery day)", "date": str(pd.Timestamp(mid).date()) if mid is not None else None,
                 "note": "later days carry this price forward"},
        "fx": {"label": "USD/CAD (Bank of Canada)", "date": str(pd.Timestamp(fx).date()) if fx is not None else None,
               "note": "weekends/holidays use the last rate"},
    }


# ---------------------------------------------------------------- main
def main():
    full = "--backfill" in sys.argv
    check_tz()
    if "--load-history" in sys.argv:
        os.makedirs(D, exist_ok=True)
        return load_history()
    os.makedirs(D, exist_ok=True)
    meta = json.load(open(META)) if os.path.exists(META) else {}
    aud = load_audit()
    seed = "--seed-audit" in sys.argv
    h = pd.DataFrame(columns=["us_mw", "us_src", "ab_mw", "ab_src", "pool_cad"], index=pd.DatetimeIndex([], tz="UTC")) if full else load_hourly()
    now_utc = pd.Timestamp.now(tz="UTC").floor("h")
    since = START.tz_convert("UTC") if full else now_utc - pd.Timedelta(days=10)
    today = dt.date.today()

    # 1. BC Hydro official hourly XLS (check at most every 6 h; download only if changed)
    if full or seed or meta.get("xls_checked", "") < (now_utc - pd.Timedelta(hours=6)).isoformat():
        try:
            links = bch_xls_links()
            for y in ([2025, today.year] if (full or seed) else sorted({today.year, (today - dt.timedelta(days=45)).year})):
                if y not in links:
                    continue
                hs = None if (full or seed) else head_size(links[y])
                if hs is not None and meta.get(f"xls_{y}") == hs:
                    continue  # unchanged per HEAD; skip the multi-MB download
                raw = get(links[y])
                sig = f"{len(raw)}"
                x = parse_bch_xls(raw)
                aud = audit_put(aud, x["us"], "us_bch")
                aud = audit_put(aud, x["ab"], "ab_bch")
                if not full and meta.get(f"xls_{y}") == sig:
                    continue
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
        e = eia_us(START.tz_convert("UTC") if seed else max(since, bch_end - pd.Timedelta(hours=1)))
        aud = audit_put(aud, e, "us_eia")  # kept even where BC Hydro official data wins, for the EIA-vs-BCH check
        h = merge_flow(h, e, "us", "eia")
    except Exception as ex:
        log("EIA failed:", ex)

    # 3. BC Hydro 5-minute page (rolling 7 days, both ties)
    live = {}
    try:
        m5, latest = bch_5min()
        aud = audit_put(aud, m5["us"], "us_bch5_raw")
        aud = audit_put(aud, m5["ab"], "ab_bch5")
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
                aud = audit_put(aud, s, "ab_aeso")
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
    aud = aud[aud.index >= START.tz_convert("UTC")].sort_index().dropna(how="all")
    aud.index.name = "t_utc"
    aud.to_csv(AUDIT, date_format="%Y-%m-%dT%H:%MZ", float_format="%.1f")
    # Daily prices (Mid-C xlsx ~MBs, BoC FX) change at most daily: refresh at most hourly.
    if full or not os.path.exists(PRICES) or meta.get("prices_checked", "") < (now_utc - pd.Timedelta(minutes=55)).isoformat():
        try:
            prices = update_prices(full or not os.path.exists(PRICES))
            meta["prices_checked"] = pd.Timestamp.now(tz="UTC").isoformat()
        except Exception as ex:
            log("Price update failed, using cached:", ex)
            prices = load_prices()
    else:
        prices = load_prices()
    META_BIAS["v"] = meta.get("us5_bias")
    # 5. BC load (BC Hydro BA hourly; incremental) and StatCan monthly generation (cross-check, daily)
    try:
        lo = update_load(meta, full, now_utc)
    except Exception as ex:
        log("BC load failed, using cached:", ex)
        lo = load_series()
    statcan = update_statcan(meta, now_utc)
    df, last_midc = compute(h, prices)
    df["load_mw"] = lo[~lo.index.duplicated(keep="last")].reindex(df.index)
    dash = build_dashboard(df, last_midc, live, source_qa(aud), statcan)
    dash["data_through"] = data_through(h, prices, live, lo)
    json.dump(dash, open(f"{D}/dashboard.json", "w"), indent=1, default=str)
    json.dump(meta, open(META, "w"), indent=1)
    for k, p in dash["periods"].items():
        t = p["total"]
        log(f"{p['label']:>18}: net {t['net_mwh']:>12,} MWh  net C${t['net_cad']:>14,}  (US {p['us']['net_cad']:,} / AB {p['ab']['net_cad']:,})")
        a = dash["arbitrage_estimate"]["periods"][k]
        log(f"{'':>18}  of which timing C${a['base']['timing_cad']:,}, matched-volume margin C${a['base']['spread_cad']:,} | peak-only: net C${a['peak_only']['net_cad']:,} timing C${a['peak_only']['timing_cad']:,} margin C${a['peak_only']['spread_cad']:,}")
        log(f"{'':>18}  completeness {p['completeness']}")
    for k, p in dash["periods"].items():
        log(f"{p['label']:>18}: load {json.dumps(p['load'])}")
    lc = dash["load_context"]
    log("load records:", json.dumps(lc["records"]), "| record peak:", json.dumps(lc["record_peak"]))
    log("StatCan check:", json.dumps({k: v for k, v in lc["statcan_check"].items() if k != "months"}))
    log("FY2026 different measures:", dash["reported_context"]["different_measures_fy2026"])
    log("source QA:", json.dumps(dash["source_qa"]))
    log("data through:", json.dumps({k: v.get("t") or v.get("date") for k, v in dash["data_through"].items()}))


if __name__ == "__main__":
    main()
