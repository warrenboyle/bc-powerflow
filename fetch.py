"""BC Hydro intertie flow + estimated wholesale dollar value (proof of concept).
Sources: EIA Hourly Electric Grid Monitor (BPAT<->BCHA interchange), EIA/ICE daily Mid-C prices,
Bank of Canada USD/CAD, and AESO API (pool price + BC intertie flow) when AESO_API_KEY is set.
Dollar figures are market-price ESTIMATES (MW x hours x hub price), not contract settlements."""
import os, json, io, datetime as dt, urllib.request
import pandas as pd

EIA_KEY = os.environ.get("EIA_API_KEY", "DEMO_KEY")
AESO_KEY = os.environ.get("AESO_API_KEY")
UA = {"User-Agent": "bc-powerflow-poc"}

def get(url, headers=None):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    return urllib.request.urlopen(req, timeout=60).read()

def fx_usdcad():
    j = json.loads(get("https://www.bankofcanada.ca/valet/observations/FXUSDCAD/json?recent=1"))
    o = j["observations"][-1]
    return float(o["FXUSDCAD"]["v"]), o["d"]

def eia_bc_us(hours=72):
    start = (dt.datetime.utcnow() - dt.timedelta(hours=hours + 48)).strftime("%Y-%m-%dT%H")
    url = ("https://api.eia.gov/v2/electricity/rto/interchange-data/data/?api_key=%s&frequency=hourly"
           "&data%%5B0%%5D=value&facets%%5Btoba%%5D%%5B%%5D=BCHA&start=%s"
           "&sort%%5B0%%5D%%5Bcolumn%%5D=period&sort%%5B0%%5D%%5Bdirection%%5D=asc&length=5000") % (EIA_KEY, start)
    d = json.loads(get(url))["response"]["data"]
    df = pd.DataFrame(d)
    df["value"] = pd.to_numeric(df["value"])
    # EIA: value = MWh flowing fromba -> toba. BPAT->BCHA negative means BC exporting to the US.
    s = df.groupby("period")["value"].sum()
    out = pd.DataFrame({"bc_export_mw": -s})
    out.index = pd.to_datetime(out.index, format="%Y-%m-%dT%H", utc=True)
    return out.tail(hours)

def midc_price():
    year = dt.date.today().year
    df = pd.read_excel(io.BytesIO(get(f"https://www.eia.gov/electricity/wholesale/xls/ice_electric-{year}.xlsx")))
    m = df[df["Price hub"].astype(str).str.contains("Mid C", case=False)].sort_values("Trade date")
    r = m.iloc[-1]
    return float(r["Wtd avg price $/MWh"]), str(r["Price hub"]), str(pd.Timestamp(r["Delivery start date"]).date())

def aeso():
    if not AESO_KEY:
        return None
    h = {"API-KEY": AESO_KEY}
    today = dt.date.today().isoformat()
    pp = json.loads(get(f"https://apimgw.aeso.ca/public/poolprice-api/v1.1/price/poolPrice?startDate={today}", h))
    csd = json.loads(get("https://apimgw.aeso.ca/public/currentsupplydemand-api/v1/csd/summary/current", h))
    return {"pool_price": pp, "csd": csd}

def main():
    fx, fx_date = fx_usdcad()
    flows = eia_bc_us()
    price_usd, hub, price_date = midc_price()
    price_cad = price_usd * fx
    flows["value_cad"] = flows["bc_export_mw"] * 1.0 * price_cad  # 1-hour intervals
    tot_mwh = flows["bc_export_mw"].sum()
    result = {
        "generated_utc": dt.datetime.utcnow().isoformat(timespec="minutes") + "Z",
        "fx": {"usdcad": fx, "date": fx_date, "source": "Bank of Canada daily average (FXUSDCAD)"},
        "midc": {"usd_mwh": price_usd, "cad_mwh": round(price_cad, 2), "hub": hub, "delivery": price_date,
                  "source": "EIA, from ICE day-ahead trades (daily, peak block, a few days delayed)"},
        "bc_us": {"latest_hour_utc": flows.index[-1].isoformat(), "latest_export_mw": float(flows["bc_export_mw"].iloc[-1]),
                   "net_export_mwh": float(tot_mwh), "est_value_cad": round(float(flows["value_cad"].sum())),
                   "hours": len(flows), "source": "EIA Hourly Electric Grid Monitor, BPAT-BCHA interchange"},
        "aeso": aeso(),
        "series": [{"t": t.isoformat(), "mw": float(r.bc_export_mw), "cad": round(float(r.value_cad))}
                   for t, r in flows.iterrows()],
        "note": "Estimated value at market hub price; not BC Hydro/Powerex contract settlements. Positive = BC exporting.",
    }
    os.makedirs("docs", exist_ok=True)
    json.dump(result, open("docs/data.json", "w"), indent=1)
    print(json.dumps({k: v for k, v in result.items() if k != "series"}, indent=1))

if __name__ == "__main__":
    main()
