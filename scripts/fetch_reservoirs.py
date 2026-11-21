"""Fetch BC reservoir levels and stored-energy estimates for the dashboard.

Sources (all public, no API key):
  * BC Hydro hydromet text files (~5-min, about 8 days of history),
    https://www.bchydro.com/info/res_hydromet/data/{code}.txt
      wsc  Williston Lake near Schooler Creek (WSC 07EF003)
      dir  Dinosaur Reservoir forebay (Peace Canyon)
      cwy  Site C / Naache Mege, east of Halfway River confluence (primary)
      cfc  Site C / Naache Mege, west of Farrell Creek (backup)
      grt  Kinbasket Lake below Garrett Creek (WSC 08NB017; confirms Corps Mica)
  * USACE CWMS Data API, office NWDP (no key),
    https://cwms-data.usace.army.mil/cwms-data
      Mica (MCDB) elevation and outflow, Arrow (ARDB) elevation/storage/outflow,
      Duncan (DCDB) elevation and outflow.
  * Water Survey of Canada / ECCC (no key) for Kootenay Lake at Queens Bay,
    station 08NH064 (geodetic, GSC 1928 datum — the IJC reference gauge):
      realtime: https://api.weather.gc.ca/collections/hydrometric-realtime/items
      history:  https://wateroffice.ec.gc.ca/services/real_time_data/csv/inline

A failing source is logged and skipped. This script exits 0 unless there is a
local bug, so it never blocks the 15-minute dashboard update.

Because the hydromet files only keep about eight days, each run appends new
hourly points to docs/reservoirs_history.csv (deduped). CWMS can seed a year
of Mica/Arrow/Duncan on the first run.

Day-of-year "typical" Williston levels come from the committed static file
docs/williston_doy_median.json (WSC 07EF003 HYDAT daily means plus a documented
geodetic offset). It is not refreshed on the 15-minute job; use
--refresh-median to rebuild it.

Usage: python scripts/fetch_reservoirs.py
       python scripts/fetch_reservoirs.py --refresh-median
"""
from __future__ import annotations

import json, os, re, sys, traceback, datetime as dt, urllib.request, urllib.error, urllib.parse
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
HIST = DOCS / "reservoirs_history.csv"
SNAP = DOCS / "reservoirs.json"
MEDIAN = DOCS / "williston_doy_median.json"
TZ = "America/Vancouver"
ETA = 0.88
G = 9.81
# 1 acre-foot in m³
AF_M3 = 1233.48
# Williston ECCC assumed-datum -> geodetic, calibrated 2026-11-20
# (wsc.txt 668.859 m at 16:20 LST vs api.weather.gc.ca LEVEL 38.794 m).
WILLISTON_OFFSET_M = 630.065

UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 bc-powerflow",
    "Accept": "text/plain,application/json,*/*",
}
BCH_REF = "https://www.bchydro.com/energy-in-bc/operations/transmission-reservoir-data/previous-reservoir-elevations/peace.html"
BCH_TXT = "https://www.bchydro.com/info/res_hydromet/data/{code}.txt"
CWMS = "https://cwms-data.usace.army.mil/cwms-data/timeseries"
ECCC_DAILY = "https://api.weather.gc.ca/collections/hydrometric-daily-mean/items"
ECCC_RT = "https://api.weather.gc.ca/collections/hydrometric-realtime/items"
WO_RT = "https://wateroffice.ec.gc.ca/services/real_time_data/csv/inline"
KOOTENAY_STN = "08NH064"
# IJC 1938 Order of Approval: 6 ft of licensed storage at Queens Bay (geodetic).
# 1739.32 ft is the post-31 Mar *maximum* target, also the floor of that band;
# 1745.32 ft is the 1 Sep–7 Jan maximum. The lake can (and does) go slightly
# below 1739.32 in late winter and well above it in freshet.
IJC_BAND_MIN_FT = 1739.32
IJC_BAND_MAX_FT = 1745.32
FORTIS_LAKE = "https://secure.fortisbc.com/lakelevel/lakes.jsp"

# Official operating ranges and live storage. Linear-in-elevation % is used
# unless a storage curve is given (Mica). See assumptions in SNAP["assumptions"].
# live_m3 is authorised live volume between min and max (licence figures).
PROJECTS = {
    "williston": {
        "name": "Williston", "plant": "G.M. Shrum / W.A.C. Bennett",
        "river": "Peace", "id": "williston",
        "min_m": 642.04, "max_m": 672.08, "live_m3": 39_471.648e6,
        "wup_min_m": 654.41, "tailwater_m": 502.0,
        "lat": 56.02, "lon": -122.20,
        "note": "Licence live range 642.04–672.08 m (32 MAF). The Peace Water Use Plan often uses a higher normal minimum of 654.41 m.",
    },
    "dinosaur": {
        "name": "Dinosaur", "plant": "Peace Canyon",
        "river": "Peace", "id": "dinosaur",
        "min_m": 500.00, "max_m": 502.92, "live_m3": 24.69e6,
        "tailwater_m": 461.6,
        "lat": 56.00, "lon": -122.08,
        "note": "Re-regulating pond under Williston. Live storage is about 25 million m³ (0.02 MAF).",
    },
    "sitec": {
        "name": "Site C", "plant": "Naache Mege",
        "river": "Peace", "id": "sitec",
        "min_m": 460.0, "max_m": 461.8, "live_m3": 165e6,
        "head_m": 52.0,  # assumed rated head; not a published daily tailwater
        "lat": 56.19, "lon": -120.91,
        "note": "Run-of-river-ish: 1.8 m operating range, 165 million m³ (0.13 MAF, 0.4% of Williston). Head is an assumed 52 m.",
    },
    "mica": {
        "name": "Mica", "plant": "Kinbasket",
        "river": "Columbia", "id": "mica",
        # official USACE level points (ft, ac-ft)
        "curve_ft_acft": [(2320.0, 8_022_200), (2434.0, 15_961_200), (2475.0, 20_075_500)],
        "tailwater_ft": 1915.0,
        "lat": 52.08, "lon": -118.30,
        "note": "Storage is interpolated from official USACE elevation–storage points because the hourly storage timeseries is often empty.",
    },
    "arrow": {
        "name": "Arrow", "plant": "Hugh Keenleyside",
        "river": "Columbia", "id": "arrow",
        "min_ft": 1378.0, "max_ft": 1444.0,
        "min_acft": 227_300, "max_acft": 7_327_300,  # live 7.1 MAF
        "head_m": 22.0,  # Keenleyside is a low dam; assumed
        "lat": 49.342, "lon": -117.774,
        "note": "Local energy uses an assumed 22 m head. Most of this water's energy is realized below the border.",
    },
    "duncan": {
        "name": "Duncan", "plant": "Duncan Dam",
        "river": "Columbia", "id": "duncan",
        "min_ft": 1794.0, "max_ft": 1892.0,
        "min_acft": 24_200, "max_acft": 1_422_800,
        "head_m": None,  # storage only
        "lat": 50.25, "lon": -116.94,
        "note": "Storage only: no generators. Energy is realized downstream on the Kootenay.",
    },
    "kootenay": {
        "name": "Kootenay Lake", "plant": "Corra Linn (FortisBC)",
        "river": "Kootenay", "id": "kootenay",
        "min_ft": IJC_BAND_MIN_FT, "max_ft": IJC_BAND_MAX_FT,
        "head_m": None,  # natural lake; energy is realized at Corra Linn and the downstream plants
        "lat": 49.654, "lon": -116.930,
        "note": "Natural lake regulated by Corra Linn (FortisBC) under the IJC 1938 Order of Approval. Percent is of the 6-ft IJC band (1739.32–1745.32 ft at Queens Bay). The dashed mark is the Order maximum for this date, not a historical median. No stored-energy figure: the lake is not a simple plant head-pond.",
    },
}

# History file: one hourly row per project, long format.
HIST_COLS = ["t_utc", "project", "elev_m", "elev_ft", "storage_acft", "outflow_cfs", "source"]


def log(*a):
    print("reservoirs:", *a, file=sys.stderr, flush=True)


def get(url, headers=None, timeout=60):
    h = {**UA, **(headers or {})}
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def ft_to_m(ft):
    return None if ft is None else float(ft) * 0.3048


def m_to_ft(m):
    return None if m is None else float(m) / 0.3048


def short_source(source):
    """Keep history.csv small: store a short tag, not the full request URL."""
    if not source:
        return ""
    s = str(source)
    # hydromet
    import re
    m = re.search(r"/data/([a-z0-9]+)\.txt", s)
    if m:
        return f"hydromet:{m.group(1)}"
    if s.startswith("hydromet:"):
        return s.split(";")[0].strip()
    # CWMS timeseries name=
    m = re.search(r"[?&]name=([^&]+)", s)
    if m:
        name = urllib.parse.unquote(m.group(1))
        return f"cwms:{name}"
    if s.startswith("cwms:"):
        return s.split(";")[0].strip()
    if "08NH064" in s or "STATION_NUMBER=08NH064" in s or "stations[]=08NH064" in urllib.parse.unquote(s):
        return "wsc:08NH064"
    if s.startswith("wsc:"):
        return s.split(";")[0].strip()
    # already short, or a joined list
    parts = []
    for part in s.split(";"):
        part = part.strip()
        if not part:
            continue
        if part.startswith("hydromet:") or part.startswith("cwms:") or part.startswith("wsc:"):
            parts.append(part)
        else:
            parts.append(short_source(part) or part[:40])
    return "; ".join(dict.fromkeys(parts))

def as_utc(t):
    """Parse a timestamp to UTC, whether it is already tz-aware or not."""
    t = pd.Timestamp(t)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")



def lerp_curve(x, pts):
    """Piecewise-linear y for x on a sorted (x, y) list. None if x is outside or None."""
    if x is None or not pts:
        return None
    pts = sorted(pts)
    if x <= pts[0][0]:
        return pts[0][1] if x == pts[0][0] else None
    if x >= pts[-1][0]:
        return pts[-1][1] if x == pts[-1][0] else None
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= x <= x1:
            if x1 == x0:
                return y0
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return None


def energy_gwh(volume_m3, head_m, eta=ETA):
    """E = ρ g h V η / 3.6e12 GWh. None if inputs missing or non-positive."""
    if volume_m3 is None or head_m is None or volume_m3 <= 0 or head_m <= 0:
        return None
    return 1000.0 * G * head_m * volume_m3 * eta / 3.6e12


# ------------------------------------------------------------------ hydromet
def parse_hydromet(text, code):
    """Return a DataFrame of timestamp (tz-aware BC) and elev_m from a hydromet .txt file."""
    rows = []
    for line in text.splitlines():
        m = re.match(r"(\d{4}/\d{2}/\d{2})\s+(\d{2}:\d{2}:\d{2})\s+([0-9]+(?:\.[0-9]+)?)", line.strip())
        if m:
            t = pd.Timestamp(f"{m.group(1).replace('/', '-')} {m.group(2)}")
            rows.append((t, float(m.group(3))))
    if not rows:
        raise ValueError(f"{code}.txt: no elevation rows parsed")
    s = pd.DataFrame(rows, columns=["t_naive", "elev_m"]).drop_duplicates("t_naive")
    # Files are labelled (PCT). From Nov 2026 BC is permanently UTC-7; America/Vancouver with tzdata >= 2026 matches.
    s["t_bc"] = s["t_naive"].dt.tz_localize(TZ, ambiguous="infer", nonexistent="shift_forward")
    return s.drop(columns=["t_naive"]).sort_values("t_bc")


def fetch_hydromet(code):
    url = BCH_TXT.format(code=code)
    raw = get(url, headers={"Referer": BCH_REF}, timeout=45)
    if raw.lstrip().startswith(b"<"):
        raise ValueError(f"{code}.txt looks like HTML (blocked or missing), {len(raw)} bytes")
    text = raw.decode("utf-8", "replace")
    # sanity: first rows should be near official ranges
    return parse_hydromet(text, code), url


def to_hourly(df, elev_col="elev_m"):
    """Last observation in each BC-local hour, indexed by hour beginning in UTC."""
    x = df.set_index("t_bc").sort_index()
    h = x.resample("h").last().dropna(subset=[elev_col])
    h = h.reset_index()
    h["t_utc"] = h["t_bc"].dt.tz_convert("UTC")
    return h


# ------------------------------------------------------------------ CWMS
def cwms_timeseries(name, office="NWDP", units=None, begin=None, end=None, page_size=20000):
    """Return a DataFrame with t_utc and value (float, NaNs dropped)."""
    end = end or pd.Timestamp.now(tz="UTC")
    begin = begin or (end - pd.Timedelta(days=400))
    q = {
        "name": name, "office": office,
        "begin": begin.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "page-size": str(page_size),
    }
    if units:
        q["units"] = units
    url = CWMS + "?" + "&".join(f"{k}={urllib.request.quote(str(v))}" for k, v in q.items())
    # quote already applied per value; rebuild properly
    url = CWMS + "?" + urllib.parse.urlencode(q)
    raw = get(url, timeout=90)
    j = json.loads(raw.decode())
    vals = (j.get("values") or j.get("time-series", {}).get("values")
            or (j.get("time-series") or {}).get("values") or [])
    # CWMS often wraps as {"values": [[ms, value, quality], ...]} or under time-series-list
    if not vals and "time-series-list" in j:
        for ts in j["time-series-list"] or []:
            vals = ts.get("values") or []
            if vals:
                break
    if not vals and isinstance(j.get("time-series"), list):
        for ts in j["time-series"]:
            vals = (ts or {}).get("values") or []
            if vals:
                break
    # Some responses use {"values": {"values": [...]}}
    if isinstance(vals, dict):
        vals = vals.get("values") or []
    rows = []
    for item in vals:
        if not item:
            continue
        t, v = item[0], item[1] if len(item) > 1 else None
        if v is None:
            continue
        if isinstance(t, (int, float)):
            ts = pd.Timestamp(t, unit="ms", tz="UTC")
        else:
            ts = pd.to_datetime(t, utc=True)
        rows.append((ts, float(v)))
    if not rows:
        raise ValueError(f"CWMS {name}: no numeric values (bytes={len(raw)})")
    out = pd.DataFrame(rows, columns=["t_utc", "value"]).drop_duplicates("t_utc").sort_values("t_utc")
    return out, url


def hourly_from_utc(df, value_col="value"):
    x = df.set_index("t_utc").sort_index()
    h = x.resample("h").last().dropna(subset=[value_col]).reset_index()
    return h


# ------------------------------------------------------------------ history
def load_history():
    if not HIST.exists():
        return pd.DataFrame(columns=HIST_COLS)
    h = pd.read_csv(HIST)
    if "t_utc" in h.columns:
        h["t_utc"] = pd.to_datetime(h["t_utc"], utc=True)
    return h.reindex(columns=HIST_COLS)


def save_history(h):
    # Merge columns for the same hour (elev + storage + outflow) instead of
    # keeping only the last source row, which would wipe outflow/storage on
    # an incremental elev-only refresh.
    h = combine_duplicate_hours(h)
    h = h.dropna(subset=["t_utc", "project"]).copy()
    h = h.sort_values(["project", "t_utc"])
    tmp = HIST.with_suffix(".csv.tmp")
    h.to_csv(tmp, index=False, date_format="%Y-%m-%dT%H:%MZ", float_format="%.4f")
    tmp.replace(HIST)


def append_points(hist, rows):
    """rows: iterable of dicts with HIST_COLS."""
    add = pd.DataFrame(list(rows), columns=HIST_COLS)
    if add.empty:
        return hist
    return pd.concat([hist, add], ignore_index=True)


def point(t_utc, project, elev_m=None, elev_ft=None, storage_acft=None, outflow_cfs=None, source=""):
    if elev_m is None and elev_ft is not None:
        elev_m = ft_to_m(elev_ft)
    if elev_ft is None and elev_m is not None:
        elev_ft = m_to_ft(elev_m)
    return {
        "t_utc": t_utc, "project": project,
        "elev_m": None if elev_m is None else round(float(elev_m), 4),
        "elev_ft": None if elev_ft is None else round(float(elev_ft), 3),
        "storage_acft": None if storage_acft is None else round(float(storage_acft), 1),
        "outflow_cfs": None if outflow_cfs is None else round(float(outflow_cfs), 1),
        "source": short_source(source),
    }


# ------------------------------------------------------------------ derived
def mica_storage_acft(elev_ft):
    return lerp_curve(elev_ft, PROJECTS["mica"]["curve_ft_acft"])


def mica_live(elev_ft):
    p = PROJECTS["mica"]
    lo, hi = p["curve_ft_acft"][0][1], p["curve_ft_acft"][-1][1]
    s = mica_storage_acft(elev_ft)
    if s is None:
        return None, None, None
    live = s - lo
    cap = hi - lo
    pct = 100.0 * live / cap if cap else None
    return live, cap, pct


def linear_live(elev, min_v, max_v, live_cap):
    if elev is None or max_v == min_v:
        return None, live_cap, None
    frac = (elev - min_v) / (max_v - min_v)
    frac = max(0.0, min(1.2, frac))  # allow a little surcharge, don't go wildly negative
    return frac * live_cap, live_cap, 100.0 * (elev - min_v) / (max_v - min_v)


def mid_slice_head(elev, min_elev, tailwater):
    """Average head of the water now in live storage, assuming constant tailwater."""
    if None in (elev, min_elev, tailwater):
        return None
    h_now = elev - tailwater
    h_min = min_elev - tailwater
    if h_now <= 0:
        return None
    return (h_now + max(h_min, 0)) / 2.0


def latest_row(hist, project):
    """Latest hour for a project, with storage/outflow carried forward up to 24 h.

    Arrow storage often lags elevation by an hour; using the bare last row
    would flip % full from a storage-based figure to a linear-elevation one.
    """
    sub = hist[hist["project"] == project].dropna(subset=["t_utc"])
    if sub.empty:
        return None
    sub = sub.sort_values("t_utc")
    last = sub.iloc[-1].copy()
    t = last["t_utc"]
    window = sub[(sub["t_utc"] >= t - pd.Timedelta(hours=24)) & (sub["t_utc"] <= t)]
    for col in ("storage_acft", "outflow_cfs", "elev_m", "elev_ft"):
        if _num(last.get(col)) is None:
            filled = window[window[col].notna()]
            if len(filled):
                last[col] = filled.iloc[-1][col]
    return last


def value_at(hist, project, t, col="elev_m"):
    sub = hist[(hist["project"] == project) & (hist["t_utc"] <= t) & hist[col].notna()]
    if sub.empty:
        return None, None
    r = sub.sort_values("t_utc").iloc[-1]
    return r[col], r["t_utc"]


def change_7d(hist, project, now_elev, now_t, col="elev_m"):
    if now_elev is None or now_t is None:
        return None
    target = as_utc(now_t) - pd.Timedelta(days=7)
    prev, prev_t = value_at(hist, project, target + pd.Timedelta(hours=12), col)
    # accept a point within 36 hours of the 7-day mark
    if prev is None or prev_t is None:
        return None
    if abs((prev_t - target).total_seconds()) > 36 * 3600:
        return None
    return {"from": float(prev), "at": prev_t.isoformat(), "delta": float(now_elev) - float(prev)}


def ijc_max_ft(t_bc):
    """IJC 1938 Order of Approval maximum at Queens Bay, in feet, for a BC-local timestamp.

    Returns (max_ft, note). After 31 March the Order's default maximum is 1739.32 ft;
    a calculated higher freshet limit may apply, and after the peak recedes the
    control is 1743.32 ft at Nelson until 1 September. Those calculated/Nelson
    limits are not modelled here.
    """
    t = pd.Timestamp(t_bc)
    if t.tzinfo is not None:
        t = t.tz_convert(TZ)
    day = pd.Timestamp(year=t.year, month=t.month, day=t.day)
    y, md = day.year, (day.month, day.day)
    def at(m, d):
        return pd.Timestamp(year=y, month=m, day=d)
    if md >= (9, 1) or md <= (1, 7):
        return IJC_BAND_MAX_FT, "IJC maximum 1 Sep–7 Jan (1745.32 ft)"
    if md <= (1, 31):
        span = (at(1, 31) - at(1, 7)).days
        frac = (day - at(1, 7)).days / span if span else 0
        return 1745.32 + (1744.00 - 1745.32) * frac, "IJC maximum, 7–31 Jan drawdown"
    last_feb = at(3, 1) - pd.Timedelta(days=1)
    if day.month == 2:
        span = (last_feb - at(1, 31)).days
        frac = (day - at(1, 31)).days / span if span else 0
        return 1744.00 + (1742.40 - 1744.00) * frac, "IJC maximum, 31 Jan–end Feb drawdown"
    if day.month == 3:
        span = (at(3, 31) - last_feb).days
        frac = (day - last_feb).days / span if span else 0
        return 1742.40 + (1739.32 - 1742.40) * frac, "IJC maximum, end Feb–31 Mar drawdown"
    return IJC_BAND_MIN_FT, ("IJC default maximum after 31 Mar (1739.32 ft); a calculated "
                             "higher freshet limit may apply, then 1743.32 ft at Nelson until 1 Sep")


def williston_typical(t_bc):

    if not MEDIAN.exists() or t_bc is None:
        return None
    try:
        j = json.load(open(MEDIAN))
        key = pd.Timestamp(t_bc).strftime("%m-%d")
        # 29 Feb -> 28 Feb if missing
        med = (j.get("median_geodetic_m") or {}).get(key)
        if med is None and key == "02-29":
            med = (j.get("median_geodetic_m") or {}).get("02-28")
        if med is None:
            return None
        p = PROJECTS["williston"]
        pct = 100.0 * (med - p["min_m"]) / (p["max_m"] - p["min_m"])
        return {"elev_m": round(float(med), 2), "pct": round(pct, 1),
                "source": "docs/williston_doy_median.json",
                "offset_m": j.get("offset_m"), "note": j.get("offset_note")}
    except Exception as ex:
        log("median unreadable:", ex)
        return None


def describe_project(hist, key):
    p = PROJECTS[key]
    row = latest_row(hist, key)
    out = {
        "id": key, "name": p["name"], "plant": p["plant"], "river": p["river"],
        "lat": p["lat"], "lon": p["lon"], "note": p["note"],
        "as_of": None, "elev_m": None, "elev_ft": None, "pct": None,
        "live_m3": None, "live_acft": None, "live_capacity_m3": None,
        "gwh": None, "d7": None, "outflow_cfs": None, "source": None,
        "typical": None, "ok": False,
    }
    if row is None:
        out["reason"] = "no data in this run or in history"
        return out
    t = row["t_utc"]
    elev_m, elev_ft = _num(row.get("elev_m")), _num(row.get("elev_ft"))
    storage = _num(row.get("storage_acft"))
    out.update({"as_of": t.isoformat(), "elev_m": _r(elev_m, 3), "elev_ft": _r(elev_ft, 2),
                "outflow_cfs": _r(_num(row.get("outflow_cfs")), 0), "source": row.get("source"), "ok": True})

    gwh = None
    if key == "mica":
        live_acft, cap_acft, pct = mica_live(elev_ft)
        out["pct"] = _r(pct, 1)
        out["live_acft"] = _r(live_acft, 0)
        out["live_capacity_acft"] = _r(cap_acft, 0)
        out["live_m3"] = _r(None if live_acft is None else live_acft * AF_M3, 0)
        out["live_capacity_m3"] = _r(None if cap_acft is None else cap_acft * AF_M3, 0)
        head = mid_slice_head(elev_ft, p["curve_ft_acft"][0][0], p["tailwater_ft"])
        if head is not None:
            head = ft_to_m(head)
        gwh = energy_gwh(out["live_m3"], head)
        out["head_m_mid_slice"] = _r(head, 1)
        out["min_ft"], out["max_ft"] = p["curve_ft_acft"][0][0], p["curve_ft_acft"][-1][0]
    elif key in ("arrow", "duncan"):
        min_ft, max_ft = p["min_ft"], p["max_ft"]
        min_af, max_af = p["min_acft"], p["max_acft"]
        cap = max_af - min_af
        if storage is not None:
            live_af = storage - min_af
        else:
            live_af, cap, _ = linear_live(elev_ft, min_ft, max_ft, cap)
            if live_af is not None:
                storage = live_af + min_af
        pct = 100.0 * (storage - min_af) / cap if (storage is not None and cap) else (
            100.0 * (elev_ft - min_ft) / (max_ft - min_ft) if elev_ft is not None else None)
        out["pct"] = _r(pct, 1)
        out["live_acft"] = _r(None if storage is None else storage - min_af, 0)
        out["live_capacity_acft"] = _r(cap, 0)
        live_m3 = None if out["live_acft"] is None else out["live_acft"] * AF_M3
        out["live_m3"] = _r(live_m3, 0)
        out["live_capacity_m3"] = _r(cap * AF_M3, 0)
        out["min_ft"], out["max_ft"] = min_ft, max_ft
        if p.get("head_m"):
            gwh = energy_gwh(live_m3, p["head_m"])
            out["head_m"] = p["head_m"]
    elif key == "kootenay":
        min_ft, max_ft = p["min_ft"], p["max_ft"]
        pct = None if elev_ft is None or max_ft == min_ft else 100.0 * (elev_ft - min_ft) / (max_ft - min_ft)
        out["pct"] = _r(pct, 1)
        out["min_ft"], out["max_ft"] = min_ft, max_ft
        t_bc = t.tz_convert(TZ)
        mx, note = ijc_max_ft(t_bc)
        typ_pct = 100.0 * (mx - min_ft) / (max_ft - min_ft)
        out["typical"] = {
            "elev_ft": _r(mx, 2), "elev_m": _r(ft_to_m(mx), 3),
            "pct": _r(typ_pct, 1),
            "source": "IJC 1938 Order of Approval rule curve",
            "note": note,
        }
        out["ijc_max_ft"] = _r(mx, 2)
        out["ft_below_ijc_max"] = _r(None if elev_ft is None else mx - elev_ft, 2)
    else:
        # metre-based Peace projects: linear in elevation
        live_m3, cap, pct = linear_live(elev_m, p["min_m"], p["max_m"], p["live_m3"])
        out["pct"] = _r(pct, 1)
        out["live_m3"] = _r(live_m3, 0)
        out["live_capacity_m3"] = _r(cap, 0)
        out["live_acft"] = _r(None if live_m3 is None else live_m3 / AF_M3, 0)
        out["live_capacity_acft"] = _r(None if cap is None else cap / AF_M3, 0)
        out["min_m"], out["max_m"] = p["min_m"], p["max_m"]
        if p.get("head_m"):
            gwh = energy_gwh(live_m3, p["head_m"])
            out["head_m"] = p["head_m"]
        elif p.get("tailwater_m") is not None:
            head = mid_slice_head(elev_m, p["min_m"], p["tailwater_m"])
            gwh = energy_gwh(live_m3, head)
            out["head_m_mid_slice"] = _r(head, 1)
        if p.get("wup_min_m") and elev_m is not None:
            out["pct_wup"] = _r(100.0 * (elev_m - p["wup_min_m"]) / (p["max_m"] - p["wup_min_m"]), 1)

    out["gwh"] = _r(gwh, 0 if gwh and gwh >= 100 else 1)
    # 7-day change in elevation (metres) and percentage points
    d7e = change_7d(hist, key, elev_m, t, "elev_m")
    if d7e:
        # percent-point change: need previous pct. Recompute simply from elev.
        prev_pct = None
        if d7e["from"] is not None:
            prev_t = as_utc(d7e["at"]) if d7e.get("at") else None
            prev_row = None
            if prev_t is not None:
                prev_sub = hist[(hist["project"] == key) & (hist["t_utc"] == prev_t)]
                if len(prev_sub):
                    prev_row = prev_sub.iloc[-1]
            if key == "mica":
                _, _, prev_pct = mica_live(m_to_ft(d7e["from"]))
            elif key in ("arrow", "duncan"):
                stor = _num(prev_row.get("storage_acft")) if prev_row is not None else None
                cap = p["max_acft"] - p["min_acft"]
                if stor is not None and cap:
                    prev_pct = 100.0 * (stor - p["min_acft"]) / cap
                else:
                    prev_pct = 100.0 * (m_to_ft(d7e["from"]) - p["min_ft"]) / (p["max_ft"] - p["min_ft"])
            elif key == "kootenay":
                prev_pct = 100.0 * (m_to_ft(d7e["from"]) - p["min_ft"]) / (p["max_ft"] - p["min_ft"])
            else:
                prev_pct = 100.0 * (d7e["from"] - p["min_m"]) / (p["max_m"] - p["min_m"])
        out["d7"] = {
            "elev_m": _r(d7e["delta"], 3),
            "pct_pt": _r(None if prev_pct is None or out["pct"] is None else out["pct"] - prev_pct, 1),
            "from_as_of": d7e["at"],
        }
    if key == "williston":
        t_bc = t.tz_convert(TZ)
        typ = williston_typical(t_bc)
        if typ:
            out["typical"] = typ
    return out


def _num(x):
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _r(x, nd=1):
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    return round(float(x), nd)


def weekly_series(hist, project, how="elev"):
    """Weekly last % for the chart; returns {t, pct} list or []."""
    sub = hist[hist["project"] == project].dropna(subset=["t_utc"]).sort_values("t_utc")
    if sub.empty:
        return []
    s = sub.set_index("t_utc")
    w = s.resample("W").last().dropna(how="all")
    out = []
    for t, r in w.iterrows():
        d = describe_one_row(project, r, t)
        if d and d.get("pct") is not None:
            out.append({"t": t.isoformat(), "pct": d["pct"]})
    return out



def daily_series(hist, project, days=120):
    """Daily last % for the last `days` days; returns {t, pct} list or []."""
    sub = hist[hist["project"] == project].dropna(subset=["t_utc"]).sort_values("t_utc")
    if sub.empty:
        return []
    end = sub["t_utc"].max()
    sub = sub[sub["t_utc"] >= end - pd.Timedelta(days=days)]
    s = sub.set_index("t_utc")
    w = s.resample("D").last().dropna(how="all")
    out = []
    for t, r in w.iterrows():
        d = describe_one_row(project, r, t)
        if d and d.get("pct") is not None:
            out.append({"t": t.isoformat(), "pct": d["pct"]})
    return out


def describe_one_row(key, r, t):
    # lightweight pct from a history row
    fake = pd.DataFrame([{**{c: r.get(c) for c in HIST_COLS}, "t_utc": t, "project": key}])
    # too heavy to call describe_project on a 1-row hist that loses outflow etc. Just pct.
    p = PROJECTS[key]
    elev_m, elev_ft = _num(r.get("elev_m")), _num(r.get("elev_ft"))
    if elev_m is None and elev_ft is not None:
        elev_m = ft_to_m(elev_ft)
    if elev_ft is None and elev_m is not None:
        elev_ft = m_to_ft(elev_m)
    if key == "mica":
        _, _, pct = mica_live(elev_ft)
    elif key in ("arrow", "duncan", "kootenay"):
        pct = None if elev_ft is None else 100.0 * (elev_ft - p["min_ft"]) / (p["max_ft"] - p["min_ft"])
    else:
        pct = None if elev_m is None else 100.0 * (elev_m - p["min_m"]) / (p["max_m"] - p["min_m"])
    return {"pct": _r(pct, 1)}


# ------------------------------------------------------------------ fetch steps (each isolated)
def collect_hydromet(hist):
    mapping = {
        "wsc": "williston",
        "dir": "dinosaur",
        "cwy": "sitec",
        "grt": "kinbasket_gauge",  # stored under a helper key, also copied onto mica if useful
    }
    sources = {}
    for code, project in mapping.items():
        try:
            df, url = fetch_hydromet(code)
            h = to_hourly(df)
            log(f"{code}.txt: {len(df)} rows, hourly {len(h)} {h['t_bc'].min()} – {h['t_bc'].max()}  last {h.iloc[-1]['elev_m']:.3f} m")
            rows = [point(r["t_utc"], project if project != "kinbasket_gauge" else "kinbasket",
                          elev_m=r["elev_m"], source=url) for _, r in h.iterrows()]
            # Kinbasket gauge is the same pool as Mica; also file as mica if CWMS later wins by last-source,
            # so write as a second project "kinbasket" and, if mica is empty, we can fall back.
            hist = append_points(hist, rows)
            sources[code] = {"ok": True, "url": url, "n": int(len(df)),
                             "from": h["t_utc"].min().isoformat(), "to": h["t_utc"].max().isoformat(),
                             "last_elev_m": _r(float(h.iloc[-1]["elev_m"]), 3)}
        except Exception as ex:
            log(f"{code}.txt failed:", ex)
            sources[code] = {"ok": False, "error": str(ex)}
    # backup Site C gauge
    try:
        df, url = fetch_hydromet("cfc")
        h = to_hourly(df)
        log(f"cfc.txt (Site C backup): last {h.iloc[-1]['elev_m']:.3f} m")
        sources["cfc"] = {"ok": True, "url": url, "last_elev_m": _r(float(h.iloc[-1]["elev_m"]), 3),
                          "to": h["t_utc"].max().isoformat()}
        # only use cfc hours where cwy/sitec is missing
        have = set(zip(hist.loc[hist["project"] == "sitec", "t_utc"].astype(str),))
        rows = []
        for _, r in h.iterrows():
            rows.append(point(r["t_utc"], "sitec", elev_m=r["elev_m"], source=url))
        # append then drop_duplicates keep last — but cwy should win if both exist.
        # Load cwy first already; append cfc then when saving, keep="last" would prefer cfc.
        # So: only add cfc hours not already present.
        existing = set(as_utc(x) for x in hist.loc[hist["project"] == "sitec", "t_utc"])
        rows = [r for r in rows if as_utc(r["t_utc"]) not in existing]
        hist = append_points(hist, rows)
    except Exception as ex:
        log("cfc.txt failed:", ex)
        sources["cfc"] = {"ok": False, "error": str(ex)}
    return hist, sources


def collect_cwms(hist):
    sources = {}
    end = pd.Timestamp.now(tz="UTC")
    # On first run pull ~400 days; afterwards 14 days is plenty (history file keeps the rest).
    have_mica = hist["project"].eq("mica").any() if len(hist) else False
    begin = end - pd.Timedelta(days=14 if have_mica else 400)

    def pull(label, name, units, project, kind):
        # kind: elev_ft, elev_m, outflow_cfs, storage_acft
        try:
            try:
                df, url = cwms_timeseries(name, units=units, begin=begin, end=end)
            except ValueError:
                if units:
                    df, url = cwms_timeseries(name, units=None, begin=begin, end=end)
                else:
                    raise
            h = hourly_from_utc(df)
            log(f"CWMS {label}: {len(df)} pts, hourly {len(h)} {h['t_utc'].min()} – {h['t_utc'].max()} last {h.iloc[-1]['value']:.3f} {units or ''}")
            rows = []
            for _, r in h.iterrows():
                kw = {"source": url}
                if kind == "elev_ft":
                    kw["elev_ft"] = r["value"]
                elif kind == "elev_m":
                    kw["elev_m"] = r["value"]
                elif kind == "outflow_cfs":
                    kw["outflow_cfs"] = r["value"]
                elif kind == "storage_acft":
                    kw["storage_acft"] = r["value"]
                rows.append(point(r["t_utc"], project, **kw))
            return rows, {"ok": True, "url": url, "n": int(len(df)),
                          "from": h["t_utc"].min().isoformat(), "to": h["t_utc"].max().isoformat(),
                          "last": _r(float(h.iloc[-1]["value"]), 3)}
        except Exception as ex:
            log(f"CWMS {label} failed:", ex)
            return [], {"ok": False, "error": str(ex), "name": name}

    series = [
        ("mica elev", "MCDB.Elev-Forebay.Inst.1Hour.0.BCHYDRO-REV", "ft", "mica", "elev_ft"),
        ("mica outflow", "MCDB.Flow-Out.Ave.1Hour.1Hour.BCHYDRO-REV", "cfs", "mica", "outflow_cfs"),
        ("arrow elev", "ARDB.Elev-Forebay.Inst.0.0.BCHYDRO-REV", "ft", "arrow", "elev_ft"),
        ("arrow stor", "ARDB.Stor.Inst.0.0.BCHYDRO-REV", "ac-ft", "arrow", "storage_acft"),
        ("arrow outflow", "ARDB.Flow-Out.Ave.~1Day.1Day.BCHYDRO-REV", "cfs", "arrow", "outflow_cfs"),
        ("duncan elev", "DCDB.Elev-Forebay.Inst.1Hour.0.BCHYDRO-REV", "ft", "duncan", "elev_ft"),
        ("duncan outflow", "DCDB.Flow-Out.Ave.~1Day.1Day.BCHYDRO-REV", "cfs", "duncan", "outflow_cfs"),
    ]
    # Alternate outflow names if the first fails — tried inside pull.
    alt = {
        "mica outflow": [
            "MCDB.Flow-Out.Inst.1Hour.0.BCHYDRO-REV",
            "MCDB.Flow-Outflow.Ave.1Hour.1Hour.BCHYDRO-REV",
        ],
    }

    for label, name, units, project, kind in series:
        rows, meta = pull(label, name, units, project, kind)
        if not meta.get("ok") and label in alt:
            for altn in alt[label]:
                rows, meta = pull(label, altn, units, project, kind)
                if meta.get("ok"):
                    break
        sources[label] = meta
        if rows:
            # merge with existing hours for same project: combine columns
            add = pd.DataFrame(rows)
            if hist.empty:
                hist = add
                continue
            hist = pd.concat([hist, add], ignore_index=True)
            # After all pulls we'll collapse duplicates by combining non-null fields.

    hist = combine_duplicate_hours(hist)
    # If mica elev is missing but kinbasket hydromet is present, copy it across
    mica_hours = set(as_utc(x) for x in hist.loc[hist["project"] == "mica", "t_utc"]) if len(hist) else set()
    kin = hist[hist["project"] == "kinbasket"] if len(hist) else pd.DataFrame()
    extra = []
    for _, r in kin.iterrows():
        t = as_utc(r["t_utc"])
        if t not in mica_hours and pd.notna(r.get("elev_m")):
            extra.append(point(t, "mica", elev_m=r["elev_m"], source=str(r.get("source") or "") + " (Kinbasket gauge, used as Mica pool)"))
    if extra:
        hist = append_points(hist, extra)
        log(f"copied {len(extra)} Kinbasket hours onto Mica (no Corps elev for those hours)")
    return hist, sources


def combine_duplicate_hours(hist):
    if hist is None or hist.empty:
        return hist if hist is not None else pd.DataFrame(columns=HIST_COLS)
    hist["t_utc"] = pd.to_datetime(hist["t_utc"], utc=True).dt.floor("h")
    rows = []
    for (t, proj), g in hist.groupby(["t_utc", "project"], dropna=False):
        rec = {"t_utc": t, "project": proj, "elev_m": None, "elev_ft": None,
               "storage_acft": None, "outflow_cfs": None, "source": None}
        srcs = []
        for _, r in g.iterrows():
            for c in ("elev_m", "elev_ft", "storage_acft", "outflow_cfs"):
                v = r.get(c)
                if pd.notna(v) and rec[c] is None:
                    rec[c] = v
            if pd.notna(r.get("source")):
                srcs.append(str(r["source"]))
        rec["source"] = short_source("; ".join(dict.fromkeys(srcs)))
        if rec["elev_m"] is None and rec["elev_ft"] is not None:
            rec["elev_m"] = ft_to_m(rec["elev_ft"])
        if rec["elev_ft"] is None and rec["elev_m"] is not None:
            rec["elev_ft"] = m_to_ft(rec["elev_m"])
        rows.append(rec)
    return pd.DataFrame(rows, columns=HIST_COLS)



def collect_kootenay(hist):
    """Kootenay Lake at Queens Bay (WSC 08NH064), geodetic GSC 1928 datum.

    Prefers the Water Survey of Canada wateroffice unit-value CSV (hours of
    history). Falls back to the ECCC hydrometric-realtime API (~30 days).
    A failing source is logged and skipped. Optionally scrapes the FortisBC
    lake-level page for the Nelson reading and forecast (never required).
    """
    sources = {}
    end = pd.Timestamp.now(tz="UTC")
    have = hist["project"].eq("kootenay").any() if len(hist) else False
    begin = end - pd.Timedelta(days=14 if have else 400)

    def rows_from_series(times, levels_m, source):
        out = []
        for ts, lvl in zip(times, levels_m):
            if lvl is None or (isinstance(lvl, float) and pd.isna(lvl)):
                continue
            out.append(point(as_utc(ts), "kootenay", elev_m=float(lvl), source=source))
        return out

    # 1) wateroffice unit values (parameter 46 = water level), chunked
    try:
        chunks = []
        cursor = begin
        while cursor < end:
            chunk_end = min(cursor + pd.Timedelta(days=120), end)
            q = {
                "stations[]": KOOTENAY_STN,
                "parameters[]": "46",
                "start_date": cursor.strftime("%Y-%m-%d %H:%M:%S"),
                "end_date": chunk_end.strftime("%Y-%m-%d %H:%M:%S"),
            }
            url = WO_RT + "?" + urllib.parse.urlencode(q)
            raw = get(url, timeout=90)
            text = raw.decode("utf-8-sig", errors="replace")
            import io
            df = pd.read_csv(io.StringIO(text))
            # columns: ID, Date, Parameter, Value, ...
            cols = {c.lower().split("/")[0].strip(): c for c in df.columns}
            date_col = cols.get("date") or df.columns[1]
            val_col = cols.get("value") or df.columns[3]
            df["t_utc"] = pd.to_datetime(df[date_col], utc=True, errors="coerce")
            df["elev_m"] = pd.to_numeric(df[val_col], errors="coerce")
            df = df.dropna(subset=["t_utc", "elev_m"])
            chunks.append(df[["t_utc", "elev_m"]])
            cursor = chunk_end
        if chunks:
            allp = pd.concat(chunks, ignore_index=True)
            allp["t_utc"] = allp["t_utc"].dt.floor("h")
            h = allp.groupby("t_utc", as_index=False)["elev_m"].last()
            hist = append_points(hist, rows_from_series(h["t_utc"], h["elev_m"], "wsc:08NH064"))
            last = h.iloc[-1]
            log(f"Kootenay WSC 08NH064 wateroffice: {len(allp)} pts, hourly {len(h)} "
                f"{h['t_utc'].min()} – {h['t_utc'].max()} last {float(last['elev_m']):.3f} m")
            sources["kootenay_wsc"] = {
                "ok": True, "url": WO_RT, "station": KOOTENAY_STN,
                "n": int(len(allp)), "hourly": int(len(h)),
                "from": h["t_utc"].min().isoformat(), "to": h["t_utc"].max().isoformat(),
                "last_elev_m": _r(float(last["elev_m"]), 3),
            }
        else:
            raise ValueError("wateroffice returned no level rows")
    except Exception as ex:
        log("Kootenay wateroffice failed:", ex)
        sources["kootenay_wsc"] = {"ok": False, "error": str(ex)}
        # 2) ECCC realtime fallback (~30 days of 5-min)
        try:
            url = (f"{ECCC_RT}?STATION_NUMBER={KOOTENAY_STN}"
                   f"&datetime={begin.strftime('%Y-%m-%dT%H:%M:%SZ')}/{end.strftime('%Y-%m-%dT%H:%M:%SZ')}"
                   f"&limit=10000&f=json&sortby=DATETIME")
            raw = get(url, timeout=60)
            j = json.loads(raw.decode())
            feats = j.get("features") or []
            times, levels = [], []
            for f in feats:
                pr = f.get("properties") or {}
                if pr.get("LEVEL") is None or not pr.get("DATETIME"):
                    continue
                times.append(pr["DATETIME"])
                levels.append(float(pr["LEVEL"]))
            if not times:
                raise ValueError(f"ECCC realtime matched {j.get('numberMatched')} but no LEVEL values")
            tmp = pd.DataFrame({"t_utc": pd.to_datetime(times, utc=True), "elev_m": levels})
            tmp["t_utc"] = tmp["t_utc"].dt.floor("h")
            h = tmp.groupby("t_utc", as_index=False)["elev_m"].last()
            hist = append_points(hist, rows_from_series(h["t_utc"], h["elev_m"], "wsc:08NH064"))
            last = h.iloc[-1]
            log(f"Kootenay WSC 08NH064 ECCC realtime fallback: hourly {len(h)} "
                f"{h['t_utc'].min()} – {h['t_utc'].max()} last {float(last['elev_m']):.3f} m")
            sources["kootenay_eccc"] = {
                "ok": True, "url": ECCC_RT, "station": KOOTENAY_STN,
                "hourly": int(len(h)), "last_elev_m": _r(float(last["elev_m"]), 3),
                "to": h["t_utc"].max().isoformat(),
            }
        except Exception as ex2:
            log("Kootenay ECCC realtime failed:", ex2)
            sources["kootenay_eccc"] = {"ok": False, "error": str(ex2)}

    # 3) FortisBC page: Nelson reading + forecast, not used for the official level
    try:
        raw = get(FORTIS_LAKE, headers={"Referer": "https://www.fortisbc.com/in-your-community/kootenay-lake-level-monitoring/kootenay-lake-levels"}, timeout=45)
        text = raw.decode("utf-8", errors="replace")
        plain = re.sub(r"<[^>]+>", " ", text)
        plain = re.sub(r"&nbsp;|&#39;", lambda m: "'" if "39" in m.group() else " ", plain)
        plain = re.sub(r"\s+", " ", plain)
        qb = re.search(r"Queen'?s Bay\s*:\s*([0-9.]+)\s*feet\s*\(([0-9.]+)\s*meters\)\s*as of\s*([^N]+?)(?=Nelson|View|$)", plain, re.I)
        nelson = re.search(r"Nelson\s*:\s*([0-9.]+)\s*feet\s*\(([0-9.]+)\s*meters\)\s*as of\s*([^.]+?)(?=View|Lake level|$)", plain, re.I)
        forecast = re.search(r"Lake level forecast\s*-\s*(.+?)(?:Average Daily|Lake level chart|$)", plain, re.I)
        extra = {"ok": True, "url": FORTIS_LAKE}
        if qb:
            extra["queens_bay_ft"] = float(qb.group(1)); extra["queens_bay_m"] = float(qb.group(2))
            extra["as_of"] = qb.group(3).strip()
        if nelson:
            extra["nelson_ft"] = float(nelson.group(1)); extra["nelson_m"] = float(nelson.group(2))
            extra["nelson_as_of"] = nelson.group(3).strip()
        if forecast:
            extra["forecast"] = re.sub(r"\s+", " ", forecast.group(1)).strip()
        sources["kootenay_fortis"] = extra
        log("Kootenay FortisBC scrape:", {k: extra[k] for k in extra if k in ("queens_bay_ft","nelson_ft","forecast","as_of")})
    except Exception as ex:
        log("Kootenay FortisBC scrape failed:", ex)
        sources["kootenay_fortis"] = {"ok": False, "error": str(ex)}
    return hist, sources


# ------------------------------------------------------------------ median file
def refresh_median():
    """Rebuild docs/williston_doy_median.json from ECCC HYDAT daily means."""
    offset = WILLISTON_OFFSET_M
    # page by decade
    by_doy = {}
    years_seen = set()
    for start, end in [("1975-01-01", "1989-12-31"), ("1990-01-01", "2004-12-31"),
                       ("2005-01-01", "2019-12-31"), ("2020-01-01", "2026-12-31")]:
        url = (f"{ECCC_DAILY}?STATION_NUMBER=07EF003&datetime={start}/{end}"
               f"&limit=10000&f=json")
        raw = get(url, timeout=90)
        j = json.loads(raw.decode())
        feats = j.get("features") or []
        log(f"ECCC daily {start}–{end}: {j.get('numberMatched')} matched, {len(feats)} returned")
        for f in feats:
            p = f.get("properties") or {}
            if p.get("LEVEL") is None or not p.get("DATE"):
                continue
            day = str(p["DATE"])[:10]
            years_seen.add(day[:4])
            doy = day[5:]
            geo = float(p["LEVEL"]) + offset
            by_doy.setdefault(doy, []).append(geo)
    med = {k: round(float(pd.Series(v).median()), 3) for k, v in sorted(by_doy.items())}
    n = {k: len(v) for k, v in sorted(by_doy.items())}
    out = {
        "station": "07EF003",
        "station_name": "WILLISTON LAKE NEAR SCHOOLER CREEK",
        "offset_m": offset,
        "offset_note": (
            "ECCC real-time and HYDAT LEVEL for 07EF003 use an assumed gauge datum, not geodetic "
            "elevation. Offset 630.065 m was calibrated on 2026-11-20: BC Hydro wsc.txt reported "
            "668.859 m at 16:20 LST (America/Vancouver) while api.weather.gc.ca hydrometric-realtime "
            "LEVEL was 38.794 m at the same instant. Do not use the raw ECCC LEVEL as a pool elevation. "
            "The offset is assumed constant across the HYDAT record; if the datum was shifted in some "
            "years the medians could be off."
        ),
        "source": ECCC_DAILY + "?STATION_NUMBER=07EF003",
        "years_used": f"{min(years_seen)}–{max(years_seen)}" if years_seen else "",
        "n_days": sum(n.values()),
        "n_years_by_doy": n,
        "median_geodetic_m": med,
        "built_at": pd.Timestamp.now(tz="UTC").isoformat(timespec="minutes"),
    }
    DOCS.mkdir(exist_ok=True)
    with open(MEDIAN, "w") as f:
        json.dump(out, f, indent=1)
        f.write("\n")
    log(f"wrote {MEDIAN} ({len(med)} doy medians, {out['n_days']} days, years {out['years_used']})")
    return out


# ------------------------------------------------------------------ snapshot
def build_snapshot(hist, source_meta):
    projects = {}
    for key in ("williston", "dinosaur", "sitec", "mica", "arrow", "duncan", "kootenay"):
        projects[key] = describe_project(hist, key)
    total_gwh = 0.0
    n_energy = 0
    for p in projects.values():
        if p.get("gwh"):
            total_gwh += p["gwh"]
            n_energy += 1
    # traces for the chart: weekly for the long Corps record, daily (last 120 d)
    # so the shorter Peace hydromet history still has something to plot.
    series = {k: weekly_series(hist, k) for k in ("williston", "mica", "arrow", "duncan", "kootenay")}
    series_daily = {k: daily_series(hist, k, days=120) for k in
                    ("williston", "dinosaur", "sitec", "mica", "arrow", "duncan", "kootenay")}
    data_through = {}
    labels = {
        "williston": "Williston (BC Hydro hydromet, Schooler Creek)",
        "dinosaur": "Dinosaur / Peace Canyon (BC Hydro hydromet)",
        "sitec": "Site C / Naache Mege (BC Hydro hydromet)",
        "mica": "Mica / Kinbasket (Corps CWMS, confirmed by BC Hydro grt.txt)",
        "arrow": "Arrow / Keenleyside (Corps CWMS)",
        "duncan": "Duncan (Corps CWMS)",
        "kootenay": "Kootenay Lake at Queens Bay (WSC 08NH064)",
    }
    for k, lab in labels.items():
        p = projects[k]
        if p.get("as_of"):
            data_through[f"res_{k}"] = {"label": lab, "t": p["as_of"]}
    assumptions = {
        "energy": "E ≈ ρ g h V η / 3.6e12 GWh, η = 0.88. Williston and Mica use the mid-elevation of the water now in live storage (constant assumed tailwater). Site C uses an assumed 52 m rated head; Arrow uses an assumed 22 m head. Duncan has no generators. Kootenay Lake has no stored-energy figure (a natural lake; generation is at Corra Linn and the downstream plants). These are engineering estimates, not a dispatch model.",
        "percent": "Percent full is live storage as a share of the official usable range. Peace projects are linear in elevation (no public Williston storage curve). Mica uses official USACE elevation–storage points (2,320 / 2,434 / 2,475 ft). Linear-in-elevation % is slightly conservative when a reservoir is high (real area is larger at the top). Kootenay Lake percent is of the 6-ft IJC 1938 Order band (1739.32–1745.32 ft at Queens Bay); 1739.32 is a seasonal *maximum* target after 31 March, not a physical empty, so the lake can read slightly below 0% in late winter and well above 100% in freshet.",
        "williston_range": "Licensed live range 642.04–672.08 m, 32 MAF (Final Water Licence). pct_wup is the same elevation against the tighter Water Use Plan normal floor of 654.41 m.",
        "typical": "The dashed 'typical for this date' mark on Williston is the median of historical 07EF003 daily means for that month-day, plus the documented 630.065 m geodetic offset in docs/williston_doy_median.json. On Kootenay Lake the dashed mark is the IJC 1938 Order maximum for that date (1745.32 ft from 1 Sep–7 Jan, drawing down to 1739.32 ft by 31 Mar; after 31 Mar the default max is 1739.32 ft and a calculated freshet limit is not modelled). Other reservoirs have no dashed mark yet.",
        "coverage": "This is the Peace cascade (Williston, Dinosaur, Site C), the Columbia River Treaty projects in BC (Mica, Arrow, Duncan), and IJC-regulated Kootenay Lake (Corra Linn, FortisBC). It is not the whole BC Hydro system: Bridge-Coastal, Campbell River, Stave and other Lower Mainland and Island plants are omitted.",
        "kootenay": "Level is WSC 08NH064 Kootenay Lake at Queens Bay, geodetic (GSC Datum 1928 Adjustment), via the ECCC / wateroffice APIs. This is the IJC reference gauge and matches FortisBC's Queen's Bay reading. 08NJ158 is 'Kootenay Lake Outflow near Corra Linn' (assumed datum, not realtime) and is not used. FortisBC also publishes a Nelson reading on secure.fortisbc.com/lakelevel/lakes.jsp (scraped as a supplement only). CWMS CORB is Corra Linn forebay, which is not the same as Queens Bay lake level and returned no points in a test pull. No stored-energy estimate is published for the lake.",
        "eta": ETA,
    }
    return {
        "generated": pd.Timestamp.now(tz=TZ).isoformat(timespec="minutes"),
        "projects": projects,
        "total_gwh": _r(total_gwh, 0),
        "total_gwh_note": "Sum of local stored-energy estimates for projects with a head assumption (Williston, Dinosaur, Site C, Mica, Arrow). Duncan is excluded.",
        "series": series,
        "series_daily": series_daily,
        "data_through": data_through,
        "sources": source_meta,
        "assumptions": assumptions,
        "history_rows": int(len(hist)),
        "history_from": hist["t_utc"].min().isoformat() if len(hist) else None,
        "history_to": hist["t_utc"].max().isoformat() if len(hist) else None,
    }


def main():
    os.chdir(ROOT)
    DOCS.mkdir(exist_ok=True)
    if "--refresh-median" in sys.argv:
        try:
            refresh_median()
        except Exception:
            traceback.print_exc()
            # Don't fail the scheduled job if someone accidentally leaves the flag on,
            # but a deliberate refresh should be visible. Exit 0 anyway? For this flag,
            # a failure should be non-zero when run by a human. Scheduled job won't pass it.
            if sys.stdin and sys.stdin.isatty():
                sys.exit(1)
        if "--refresh-median" in sys.argv and len([a for a in sys.argv[1:] if a != "--refresh-median"]) == 0:
            return
    try:
        hist = load_history()
        source_meta = {}
        try:
            hist, s = collect_hydromet(hist)
            source_meta.update(s)
        except Exception as ex:
            log("hydromet block failed:", ex)
            source_meta["hydromet"] = {"ok": False, "error": str(ex)}
        try:
            hist, s = collect_cwms(hist)
            source_meta.update(s)
        except Exception as ex:
            log("CWMS block failed:", ex)
            source_meta["cwms"] = {"ok": False, "error": str(ex)}
        try:
            hist, s = collect_kootenay(hist)
            source_meta.update(s)
        except Exception as ex:
            log("Kootenay block failed:", ex)
            source_meta["kootenay"] = {"ok": False, "error": str(ex)}
        hist = combine_duplicate_hours(hist)
        if len(hist):
            save_history(hist)
            log(f"history {len(hist)} hourly rows, {hist['t_utc'].min()} – {hist['t_utc'].max()}")
        snap = build_snapshot(hist, source_meta)
        with open(SNAP, "w") as f:
            json.dump(snap, f, indent=1, default=str)
            f.write("\n")
        log("snapshot", SNAP, "total_gwh", snap.get("total_gwh"))
        for k, p in snap["projects"].items():
            log(f"  {k}: elev={p.get('elev_m')} m  {p.get('pct')}%  d7={p.get('d7')}  {p.get('gwh')} GWh  ok={p.get('ok')}")
    except Exception:
        traceback.print_exc()
        log("reservoir fetch failed overall; leaving previous snapshot/history in place")
        # still exit 0 so the 15-minute job continues
        return


if __name__ == "__main__":
    main()
