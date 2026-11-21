"""Record a BC Hydro peak event and snapshot the public conditions visible when it was called.

Usage (from the repo root):
  python scripts/snapshot_event.py --date 2026-11-26 --start 17:00 --end 21:00 \
      --noticed-at 2026-11-25T16:30-07:00 --source "BC Hydro Peak Saver email"

(a) Adds or updates the event in docs/events.json (same date+start+end = same event; idempotent).
(b) Appends a capture to docs/event_snapshots.json under key "YYYY-MM-DD HH:MM-HH:MM". A rerun with the same
    noticed-at and retroactive flag replaces its own capture instead of adding another.

Every field is {"value": ..., "source": ..., "as_of": ..., "reason": ...}; a source that can't be reached or has
nothing for the hours is value null with a reason. Nothing is estimated or filled in.
A capture is "retroactive" when it runs more than 2 hours after --noticed-at (or with --retroactive): then only
sources that keep history are used (Open-Meteo previous-runs forecasts, AESO history, this repo's CSVs) and
current-only sources (grid condition, supply/demand, wind/solar forecast) are recorded as null with a reason.

Env: AESO_API_KEY (optional; enables the AESO API). Without it the keyless AESO ETS reports are tried.
"""
import os, sys, io, re, csv, json, argparse, datetime as dt
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
import fetch as F  # noqa: E402  (shared HTTP helper, file paths and time zones)

EVENTS, SNAPS = "docs/events.json", "docs/event_snapshots.json"
AB_TZ = "America/Edmonton"  # AESO reports are in Mountain prevailing time
AESO_KEY = os.environ.get("AESO_API_KEY")
ETS = "http://ets.aeso.ca/ets_web/ip/Market/Reports"
WSF = "http://ets.aeso.ca/Market/Reports/Manual/Operations/prodweb_reports/wind_solar_forecast"
CITIES = {"Vancouver": (49.2827, -123.1207), "Victoria": (48.4284, -123.3656),
          "Kelowna": (49.8880, -119.4960), "Prince George": (53.9171, -122.7497)}
NOW = pd.Timestamp.now(tz="UTC")


def fld(value=None, source=None, as_of=None, reason=None, **extra):
    d = {"value": value, "source": source, "as_of": as_of}
    if value is None:
        d["reason"] = reason or "not available"
    d.update(extra)
    return d


def err(ex):
    s = f"{type(ex).__name__}: {ex}"
    return re.sub(r"(API-KEY|subscription-key)=?[^&\s]*", r"\1=***", s)[:200]


def num(x):
    try:
        x = str(x).replace(",", "").strip()
        return None if x in ("", "-", "None", "nan") else float(x)
    except ValueError:
        return None


def window(date, start, end):
    t0 = pd.Timestamp(date).tz_localize(F.TZ)
    sh, eh = int(start[:2]), (24 if end == "24:00" else int(end[:2]))
    hb = list(range(sh, eh))
    return t0, hb, [(t0 + pd.Timedelta(hours=h)).tz_convert("UTC") for h in hb]


# ------------------------------------------------------------------ events.json
def upsert_event(a):
    j = json.load(open(EVENTS)) if os.path.exists(EVENTS) else {"events": []}
    evs = j.setdefault("events", [])
    rec = next((e for e in evs if (e.get("date"), e.get("start"), e.get("end")) == (a.date, a.start, a.end)), None)
    new = rec is None
    if new:
        rec = {"date": a.date, "start": a.start, "end": a.end}
        evs.append(rec)
    if a.source:
        rec["source"] = a.source
    elif new:
        rec["source"] = ""
    if a.note:
        rec["note"] = a.note
    rec.setdefault("note", "")
    rec["noticed_at"] = a.noticed_at
    evs.sort(key=lambda e: (e.get("date", ""), e.get("start", "")))
    with open(EVENTS, "w") as f:
        json.dump(j, f, indent=1, ensure_ascii=False)
        f.write("\n")
    return new


# ------------------------------------------------------------------ AESO
def aeso_api(path):
    return json.loads(F.get(f"https://apimgw.aeso.ca/public/{path}", {"API-KEY": AESO_KEY}, timeout=60))


def ets_actual_forecast(day=None):
    """Keyless ETS Actual/Forecast report: hourly forecast pool price and forecast AIL (MPT hour-ending)."""
    url = f"{ETS}/ActualForecastWMRQHReportServlet?contentType=csv"
    if day is not None:
        url += f"&beginDate={day:%m%d%Y}&endDate={(day + dt.timedelta(days=1)):%m%d%Y}"
    rows = {}
    for r in csv.reader(io.StringIO(F.get(url, timeout=60).decode("utf-8", "replace"))):
        m = re.match(r"(\d\d/\d\d/\d{4}) (\d\d)$", r[0].strip()) if r else None
        if not m or len(r) < 5:
            continue
        he = int(m.group(2))
        t = (pd.Timestamp(dt.datetime.strptime(m.group(1), "%m/%d/%Y")).tz_localize(AB_TZ)
             + pd.Timedelta(hours=he - 1)).tz_convert("UTC")
        rows[t] = {"forecast_pool": num(r[1]), "actual_pool": num(r[2]), "forecast_ail": num(r[3]), "actual_ail": num(r[4])}
    return rows, url


def aeso_prices(date, utc_hours, retro):
    """Forecast pool price (and actual, for comparison) and forecast Alberta internal load for the event hours."""
    day_utc = pd.date_range(pd.Timestamp(date).tz_localize(F.TZ), periods=24, freq="h").tz_convert("UTC")
    rows, src, why = {}, f"{ETS}/ActualForecastWMRQHReportServlet" + ("" if AESO_KEY else " (keyless; no AESO_API_KEY)"), []
    if AESO_KEY:
        try:
            d0, d1 = (min(day_utc).tz_convert(AB_TZ).date()), (max(day_utc).tz_convert(AB_TZ).date())
            for r in aeso_api(f"poolprice-api/v1.1/price/poolPrice?startDate={d0}&endDate={d1}")["return"]["Pool Price Report"]:
                t = pd.Timestamp(r["begin_datetime_utc"], tz="UTC")
                rows.setdefault(t, {})
                rows[t].update({"forecast_pool": num(r.get("forecast_pool_price")), "actual_pool": num(r.get("pool_price"))})
            try:
                j = aeso_api(f"actualforecast-api/v1/load/albertaInternalLoad?startDate={d0}&endDate={d1}")["return"]
                lst = next((v for v in j.values() if isinstance(v, list)), []) if isinstance(j, dict) else j
                for r in lst:
                    t = pd.Timestamp(r["begin_datetime_utc"], tz="UTC")
                    fk = next((k for k in r if "forecast" in k.lower()), None)
                    ak = next((k for k in r if "load" in k.lower() and "forecast" not in k.lower() and "date" not in k.lower()), None)
                    rows.setdefault(t, {})
                    rows[t].update({"forecast_ail": num(r.get(fk)) if fk else None, "actual_ail": num(r.get(ak)) if ak else None})
            except Exception as ex:
                why.append("AESO actual/forecast API: " + err(ex))
            src = "AESO API (poolprice-api v1.1, actualforecast-api v1)"
        except Exception as ex:
            why.append("AESO API: " + err(ex))
    else:
        why.append("no AESO_API_KEY")
    if not rows:
        try:
            r1, url = ets_actual_forecast(pd.Timestamp(date).date() if retro else None)
            rows, src = r1, url
            if not r1:
                why.append("ETS Actual/Forecast report returned no rows")
        except Exception as ex:
            why.append("ETS Actual/Forecast report: " + err(ex))
    as_of = NOW.isoformat(timespec="minutes")
    note = ("AESO publishes a forecast pool price only a few hours ahead, so at notice time (usually the evening before) "
            "the event hours normally have none; forecast AIL (Alberta load) is published about a day ahead.")
    out = {}
    for k, lab in (("forecast_pool", "forecast pool price C$/MWh"), ("forecast_ail", "forecast Alberta internal load MW"),
                   ("actual_pool", "actual pool price C$/MWh")):
        vals = [rows.get(t, {}).get(k) for t in utc_hours]
        have = [v for v in vals if v is not None]
        if have:
            out[k] = fld({"hourly": vals, "avg": round(sum(have) / len(have), 2), "max": max(have), "hours": len(have)},
                         src, as_of, label=lab)
        else:
            out[k] = fld(None, src, as_of, "; ".join(why) or ("none published for the event hours at capture time" if k != "actual_pool" else "not settled yet"), label=lab)
        if k == "forecast_ail":
            dv = [rows.get(t, {}).get(k) for t in day_utc]
            dv = [v for v in dv if v is not None]
            out["forecast_ail_day_max"] = fld(max(dv) if dv else None, src, as_of, "; ".join(why) or "no forecast for the day", label="forecast AIL, day max MW")
    out["note"] = note
    if retro:
        out["retro_note"] = ("Retroactive: AESO keeps only its final forecast for each hour (made shortly before the hour), "
                             "not the forecast that existed at notice time.")
    return out


def aeso_grid_now():
    """Current-only AESO sources: grid condition, supply/demand summary (derived cushion) and the 7-day wind/solar forecast."""
    as_of = NOW.isoformat(timespec="minutes")
    out = {}
    url = "https://www.aeso.ca/grid/understanding-grid-conditions/"
    try:
        h = F.get(url, timeout=60).decode("utf-8", "replace")
        m = re.search(r'class="state level-(\d)">\s*([^<]+?)\s*</p>', h)
        d = re.search(r'class="date">\s*Updated\s*([^<]+?)\s*</div>\s*<div class="time">\s*([^<]+?)\s*</div>', h)
        out["grid_condition"] = fld(m.group(2) if m else None, url, (f"{d.group(1)} {d.group(2)}".replace(",,", ",") if d else as_of),
                                    None if m else "status not found on page", level=int(m.group(1)) if m else None)
    except Exception as ex:
        out["grid_condition"] = fld(None, url, as_of, err(ex))
    # supply / demand: AESO API (key) or ETS CSD (keyless)
    csd, src, why = None, None, []
    if AESO_KEY:
        try:
            r = aeso_api("currentsupplydemand-api/v2/csd/summary/current")["return"]
            fuels = {}
            for g in r.get("generation_data_list", []):
                fuels[g.get("fuel_type", "").upper()] = (num(g.get("aggregated_maximum_capability")), num(g.get("aggregated_net_generation")))
            csd = {"t": r.get("effective_datetime_mpt"), "ail": num(r.get("alberta_internal_load")), "fuels": fuels,
                   "interchange": num(r.get("net_actual_interchange"))}
            src = "AESO API currentsupplydemand-api v2"
        except Exception as ex:
            why.append("AESO CSD API: " + err(ex))
    if csd is None:
        u = f"{ETS}/CSDReportServlet?contentType=csv"
        try:
            rows = list(csv.reader(io.StringIO(F.get(u, timeout=60).decode("utf-8", "replace"))))
            kv = {r[0].strip(): r[1:] for r in rows if r}
            lu = next((r[0] for r in rows if r and "Last Update" in r[0]), "")
            fuels = {k.upper(): (num(v[0]), num(v[1])) for k, v in kv.items() if len(v) >= 2 and k.isupper() and k != "TOTAL"}
            csd = {"t": lu.replace("Last Update :", "").strip() + " MPT", "ail": num((kv.get("Alberta Internal Load (AIL)") or [None])[0]),
                   "fuels": fuels, "interchange": num((kv.get("Net Actual Interchange") or [None])[0])}
            src = u
        except Exception as ex:
            why.append("ETS CSD report: " + err(ex))
    if csd and csd["fuels"]:
        disp = [f for f in csd["fuels"] if f not in ("WIND", "SOLAR")]
        cush = sum((csd["fuels"][f][0] or 0) - (csd["fuels"][f][1] or 0) for f in disp)
        out["supply_cushion_derived"] = fld(round(cush), src, csd["t"], label="unused dispatchable capability MW (derived)",
                                            note="Sum of (maximum capability - net generation) for all non-wind, non-solar fuel types "
                                                 "in AESO's Current Supply Demand report. A rough stand-in: AESO does not publish its "
                                                 "supply cushion as a number, and capability includes units on outage/long lead time.")
        out["alberta_load_now"] = fld(csd["ail"], src, csd["t"], label="Alberta internal load now MW")
    else:
        out["supply_cushion_derived"] = fld(None, src, as_of, "; ".join(why) or "no data")
        out["alberta_load_now"] = fld(None, src, as_of, "; ".join(why) or "no data")
    return out


def aeso_windsolar(utc_hours, date):
    """AESO 7-day wind and solar power forecast (keyless CSVs, MPT hour-beginning, MW): most-likely values."""
    as_of = NOW.isoformat(timespec="minutes")
    out = {}
    day_utc = pd.date_range(pd.Timestamp(date).tz_localize(F.TZ), periods=24, freq="h").tz_convert("UTC")
    for k in ("wind", "solar"):
        url = f"{WSF}/{k}_rpt_longterm.csv"
        try:
            rows = {}
            for r in csv.reader(io.StringIO(F.get(url, timeout=60).decode("utf-8", "replace"))):
                if r and re.match(r"\d{4}-\d\d-\d\d \d\d:\d\d$", r[0].strip()) and len(r) >= 6:
                    t = pd.Timestamp(r[0].strip()).tz_localize(AB_TZ, ambiguous="NaT", nonexistent="NaT")
                    if not pd.isna(t):
                        rows[t.tz_convert("UTC")] = {"min": num(r[1]), "ml": num(r[2]), "max": num(r[3]), "cap": num(r[5])}
            vals = [rows.get(t, {}).get("ml") for t in utc_hours]
            have = [v for v in vals if v is not None]
            cap = next((x["cap"] for x in rows.values() if x.get("cap")), None)
            out[k] = fld({"hourly_most_likely": vals, "avg": round(sum(have) / len(have)), "min_of_low_case": min(
                rows[t]["min"] for t in utc_hours if t in rows and rows[t]["min"] is not None) if have else None,
                "capacity_mw": cap, "day_avg": round(sum(rows[t]["ml"] for t in day_utc if t in rows and rows[t]["ml"] is not None)
                                                      / max(1, sum(1 for t in day_utc if t in rows and rows[t]["ml"] is not None)))} if have else None,
                         url, as_of, "forecast does not cover the event hours" if rows else "no rows parsed",
                         label=f"Alberta {k} forecast MW (most likely)")
        except Exception as ex:
            out[k] = fld(None, url, as_of, err(ex), label=f"Alberta {k} forecast MW")
    return out


# ------------------------------------------------------------------ weather
def temps(date, hb, noticed, retro):
    """Hourly 2 m temperature (deg C) for the event window in 4 BC cities, from Open-Meteo (keyless).
    Live: the current forecast. Retroactive: the forecast issued about a day earlier (previous-runs API, *_previous_day1),
    which is the closest kept forecast to an evening-before notice; also the day-0 value as 'what happened'."""
    lat = ",".join(str(v[0]) for v in CITIES.values())
    lon = ",".join(str(v[1]) for v in CITIES.values())
    q = f"latitude={lat}&longitude={lon}&timezone=America%2FVancouver&start_date={date}&end_date={date}"
    tries = []
    if retro:
        tries.append(("https://previous-runs-api.open-meteo.com/v1/forecast?" + q + "&hourly=temperature_2m_previous_day1,temperature_2m",
                      "temperature_2m_previous_day1", "Open-Meteo previous-runs API: forecast issued ~24 h before each hour"))
    else:
        tries.append(("https://api.open-meteo.com/v1/forecast?" + q + "&hourly=temperature_2m", "temperature_2m", "Open-Meteo forecast API (current forecast)"))
        tries.append(("https://previous-runs-api.open-meteo.com/v1/forecast?" + q + "&hourly=temperature_2m", "temperature_2m",
                      "Open-Meteo previous-runs API (latest run; used because the forecast API was unreachable)"))
    why = []
    for url, key, label in tries:
        try:
            j = json.loads(F.get(url, timeout=60))
            j = j if isinstance(j, list) else [j]
            cities = {}
            for name, r in zip(CITIES, j):
                h = r["hourly"]
                idx = {t: i for i, t in enumerate(h["time"])}
                vals = [h[key][idx[f"{date}T{x:02d}:00"]] if f"{date}T{x:02d}:00" in idx else None for x in hb]
                act = [h["temperature_2m"][idx[f"{date}T{x:02d}:00"]] if retro and f"{date}T{x:02d}:00" in idx else None for x in hb]
                have = [v for v in vals if v is not None]
                cities[name] = {"hourly": vals, "min": min(have) if have else None, "avg": round(sum(have) / len(have), 1) if have else None}
                if retro:
                    ah = [v for v in act if v is not None]
                    cities[name]["day0_min"] = min(ah) if ah else None
            mins = [c["min"] for c in cities.values() if c["min"] is not None]
            avgs = [c["avg"] for c in cities.values() if c["avg"] is not None]
            if not mins:
                why.append(f"{label}: no values for the window"); continue
            return fld({"cities": cities, "min_all": min(mins), "avg_4city": round(sum(avgs) / len(avgs), 1)},
                       url, NOW.isoformat(timespec="minutes"), label=label,
                       note=("day0_min = the model's first-hours value for that time, a stand-in for what happened" if retro else None))
        except Exception as ex:
            why.append(f"{label}: {err(ex)}")
    return fld(None, tries[0][0], NOW.isoformat(timespec="minutes"), "; ".join(why))


# ------------------------------------------------------------------ this repo's data

def reservoir_context(noticed, retro):
    """Pool / % full / outflow / est. GWh at or before notice time, from the persistent history file."""
    src = "docs/reservoirs_history.csv (BC Hydro hydromet text files + USACE CWMS NWDP; hourly)"
    try:
        import importlib.util
        path = os.path.join(ROOT, "scripts", "fetch_reservoirs.py")
        spec = importlib.util.spec_from_file_location("fetch_reservoirs", path)
        R = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(R)
        if not R.HIST.exists():
            return fld(None, src, None, "no reservoir history file yet")
        hist = R.load_history()
        if hist is None or hist.empty:
            return fld(None, src, None, "reservoir history is empty")
        cut = noticed.tz_convert("UTC")
        # Use history at or before the notice so a later capture doesn't leak future levels.
        hist = hist[hist["t_utc"] <= cut].copy()
        if hist.empty:
            return fld(None, src, None, "no reservoir history at or before the notice time")
        as_of = hist["t_utc"].max()
        lag_h = (cut - as_of).total_seconds() / 3600.0
        projects = []
        for key in R.PROJECTS:
            d = R.describe_project(hist, key)
            if not d.get("ok"):
                projects.append({"id": key, "name": d.get("name"), "ok": False, "reason": d.get("reason")})
                continue
            projects.append({
                "id": key, "name": d.get("name"), "plant": d.get("plant"), "river": d.get("river"),
                "elev_m": d.get("elev_m"), "pct": d.get("pct"),
                "outflow_cfs": d.get("outflow_cfs"), "gwh": d.get("gwh"),
                "as_of": d.get("as_of"), "source": d.get("source"), "ok": True,
            })
        gs = [p["gwh"] for p in projects if p.get("gwh") is not None]
        value = {"projects": projects, "total_gwh": (round(sum(gs), 0) if gs else None)}
        extra = {}
        if lag_h > 36:
            extra["note"] = (f"Latest history hour is {lag_h:.0f} h before the notice. "
                             "Peace hydromet files only retain about eight days, so older events may lack Peace levels.")
        return fld(value, src, as_of.isoformat(), **extra)
    except Exception as ex:
        return fld(None, src, None, err(ex))


def bc_context(noticed, retro):
    out = {}
    lo = F.load_series().dropna()
    cut = noticed.tz_convert("UTC")
    L = lo[lo.index < cut] if retro else lo
    src = "docs/load_hourly.csv (BC Hydro balancing-authority hourly load)"
    if len(L):
        last_t = L.index.max()
        lastday = L[L.index.tz_convert(F.TZ).date == last_t.tz_convert(F.TZ).date()]
        p365 = L[L.index >= last_t - pd.Timedelta(days=365)]
        out["bc_load_latest_day"] = fld({"date": str(last_t.tz_convert(F.TZ).date()), "peak_mw": int(lastday.max()),
                                         "peak_pct_of_365d": round(float(lastday.max() / p365.max() * 100), 1),
                                         "through": F._t(last_t + pd.Timedelta(hours=1))}, src, NOW.isoformat(timespec="minutes"),
                                        note=("Latest hour before the notice in today's file. BC Hydro posts load a business day "
                                              "or more late and posting times aren't tracked, so this may not have been public yet." if retro else
                                              "Latest posted day at capture time."))
        out["bc_load_365d_peak"] = fld({"mw": int(p365.max()), "t": F._t(p365.idxmax())}, src, NOW.isoformat(timespec="minutes"))
    else:
        out["bc_load_latest_day"] = fld(None, src, None, "no load data")
        out["bc_load_365d_peak"] = fld(None, src, None, "no load data")
    try:
        p = F.load_prices()["midc_usd"].dropna()
        if retro:
            p = p[p.index <= pd.Timestamp(noticed.tz_convert(F.TZ).date())]
        d = p.index.max()
        out["midc_latest"] = fld({"delivery_date": str(d.date()), "usd_mwh": round(float(p.loc[d]), 2)},
                                 "docs/daily_prices.csv (EIA/ICE Mid-C day-ahead peak)", NOW.isoformat(timespec="minutes"),
                                 note=("Latest delivery day on or before the notice date in today's data; EIA publishes days late, "
                                       "so it may not have been out at notice time." if retro else "Latest delivery day published at capture time."))
    except Exception as ex:
        out["midc_latest"] = fld(None, "docs/daily_prices.csv", None, err(ex))
    out["bc_reservoirs"] = reservoir_context(noticed, retro)
    return out


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--date", required=True); ap.add_argument("--start", required=True); ap.add_argument("--end", required=True)
    ap.add_argument("--noticed-at", required=True, help="when the notice arrived, ISO 8601 with offset, e.g. 2026-11-18T17:14-07:00")
    ap.add_argument("--source", default=""); ap.add_argument("--note", default="")
    ap.add_argument("--retroactive", action="store_true", help="force retroactive mode")
    ap.add_argument("--no-snapshot", action="store_true", help="only add/update the event")
    a = ap.parse_args()
    dt.date.fromisoformat(a.date)
    for x in (a.start, a.end):
        if not re.match(r"^([01]\d|2[0-4]):00$", x):
            sys.exit(f"--start/--end must be HH:00 (got {x})")
    if a.end != "24:00" and a.end <= a.start:
        sys.exit("--end must be after --start")
    noticed = pd.Timestamp(a.noticed_at)
    if noticed.tzinfo is None:
        sys.exit("--noticed-at needs a UTC offset or Z, e.g. 2026-11-18T17:14-07:00")
    a.noticed_at = noticed.isoformat()
    new = upsert_event(a)
    F.log(("added" if new else "updated"), "event", a.date, a.start, a.end, "in", EVENTS)
    if a.no_snapshot:
        return
    retro = a.retroactive or NOW > noticed.tz_convert("UTC") + pd.Timedelta(hours=2)
    t0, hb, utc_hours = window(a.date, a.start, a.end)
    cap = {"captured_at": NOW.isoformat(timespec="minutes"), "noticed_at": a.noticed_at, "retroactive": bool(retro),
           "event_hours_utc": [t.isoformat() for t in utc_hours], "hour_ending_bc": [h + 1 for h in hb]}
    cap["alberta"] = aeso_prices(a.date, utc_hours, retro)
    if retro:
        why = "current-only source: AESO doesn't publish past values of this, so it can't be recovered for a past notice time"
        cap["alberta"].update({"grid_condition": fld(None, "https://www.aeso.ca/grid/understanding-grid-conditions/", None, why),
                               "supply_cushion_derived": fld(None, f"{ETS}/CSDReportServlet", None, why),
                               "alberta_load_now": fld(None, f"{ETS}/CSDReportServlet", None, why),
                               "wind": fld(None, f"{WSF}/wind_rpt_longterm.csv", None, why),
                               "solar": fld(None, f"{WSF}/solar_rpt_longterm.csv", None, why)})
    else:
        cap["alberta"].update(aeso_grid_now())
        cap["alberta"].update(aeso_windsolar(utc_hours, a.date))
    cap["weather"] = temps(a.date, hb, noticed, retro)
    cap["bc"] = bc_context(noticed, retro)
    key = f"{a.date} {a.start}-{a.end}"
    j = json.load(open(SNAPS)) if os.path.exists(SNAPS) else {}
    j.setdefault("_readme", "Conditions visible when each peak event was called, written by scripts/snapshot_event.py. "
                            "Key = 'date start-end' (BC time). Each capture: captured_at, noticed_at, retroactive, and fields "
                            "{value, source, as_of, reason}. value null = not available (see reason); nothing is estimated.")
    lst = j.setdefault("snapshots", {}).setdefault(key, [])
    lst[:] = [c for c in lst if not (c.get("noticed_at") == cap["noticed_at"] and c.get("retroactive") == cap["retroactive"])]
    lst.append(cap)
    lst.sort(key=lambda c: c["captured_at"])
    with open(SNAPS, "w") as f:
        json.dump(j, f, indent=1, ensure_ascii=False, default=str)
        f.write("\n")
    flat = lambda d: {k: (v.get("value") if isinstance(v, dict) and "value" in v else v) for k, v in d.items()}
    F.log(f"snapshot {key} ({'retroactive' if retro else 'live'}) ->", SNAPS)
    for sec in ("alberta", "weather", "bc"):
        v = cap[sec]
        F.log(f"  {sec}:", json.dumps(flat(v) if sec != "weather" else v.get("value") or v.get("reason"), default=str)[:1500])


if __name__ == "__main__":
    main()
