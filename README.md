# bc-powerflow

BC Hydro intertie flows (BC–US and BC–Alberta) with an estimated wholesale dollar value at hub prices: Mid-C (EIA/ICE daily) for the US tie and the AESO hourly pool price for the Alberta tie.

Dollar figures are market-price estimates (MW × hours × hub price), not BC Hydro/Powerex settlements.

Repo secrets used by the workflow (scheduled every 15 minutes, best-effort; source data lags): `EIA_API_KEY` (optional; falls back to DEMO_KEY), `AESO_API_KEY` (enables Alberta data).

Site: https://warrenboyle.github.io/bc-powerflow/

## Valuation

- **BC–US:** Mid-C day-ahead *peak* price, with off-peak hours priced at `OFFPEAK_FACTOR` × peak (env var, default **0.8**; this is an assumption, not data). Off-peak = hour-ending 23–6 Mon–Sat, all Sunday, and NERC holidays (New Year's, Memorial Day, Independence Day, Labor Day, Thanksgiving, Christmas; a Sunday holiday is observed on Monday, Saturday holidays are not moved), in **US Pacific prevailing time**. The Mid-C delivery day is also taken in US Pacific time. Converted with Bank of Canada daily USD/CAD.
- **BC–Alberta:** AESO hourly pool price (C$).
- A **peak-price-only** alternative (every US hour at the peak index) is kept as `peak_only` in `docs/dashboard.json` and shown in small text on the cards.
- *Future improvement:* apply an hourly shape from a free hourly market (e.g. CAISO OASIS) scaled to the Mid-C daily index, instead of the flat 0.8× off-peak factor.

## Time zones

BC has been on permanent UTC−7 since November 2026 (BC Reg. 20/2026). BC-local grouping, display and the BC Hydro hourly files use `America/Vancouver`, which needs tzdata ≥ 2026 (permanent UTC−7 from 2026-11-01, historical DST before). The workflow pins the `tzdata` package and sets `PYTHONTZPATH=""`; `fetch.py` checks the rules at start-up and aborts the run (leaving the last good data live) if the tz database is older. Mid-C days and peak/off-peak blocks use `America/Los_Angeles`, which still observes DST, so from November to March BC and US Pacific differ by an hour. The page shows times in BC time (fixed UTC−7 in the browser from March 2026, so older browser tz data doesn't show PST).

## Dashboard sections

- **Rolling totals** (month to date, previous month, year to date, previous year): tie-line flows valued at hub prices. This is *not* BC Hydro's import bill nor Powerex's trading margin.
- **Trading breakdown** (`arbitrage_estimate` in `docs/dashboard.json`). Both lines are **components of the total, not additions**, and they overlap each other, so don't add them to the total or to each other. Net value E·Pe − I·Pi already contains the matched-volume margin: if I > E, net = min(E,I)·(Pe − Pi) − (I − E)·Pi.
  - *Gain from better-timed trades* (`timing_cad`) = Σ hourly flow × (hourly price − that tie's simple average price for the calendar month). Alberta has true hourly prices. On the US tie Mid-C is one price per day, so this reflects day-to-day timing plus the assumed peak/off-peak split, not hour-to-hour timing.
  - *Margin on matched import/export volume* (`spread_cad`) = (flow-weighted avg export price − flow-weighted avg import price) × min(export MWh, import MWh), per month, both ties pooled.
  - Both are computed per calendar month; rolling totals are sums of months, and there is a `monthly` series.
- **Reservoirs** (`reservoirs` in `docs/dashboard.json`): teacup map and table of Peace, Columbia and Kootenay Lake storage; see [Reservoirs](#reservoirs).
- **Peak events** (`peak_events`): see [Peak events](#peak-events).
- **BC Hydro reported figures – different measures** (`reported_context`): static BC Hydro figures for FY2025 and FY2026 (imports, exports, net cost, consolidated trade revenue, and Powerex trade-income ranges), with source links, beside this site's FY2026 numbers. These are **different measures, not like-for-like**: tie-line flows include Powerex trading and other non-purchase flows valued at hub prices, while BC Hydro's reported trade is its domestic purchases and sales at Powerex transfer prices. The price ratios in `different_measures_fy2026` are descriptive only.
  - Sources: [BC Hydro 2025/26 Annual Service Plan Report](https://bcbudget.gov.bc.ca/Annual_Reports/2025_2026/pdf/agency/bchydro.pdf), [2024/25 report](https://bcbudget.gov.bc.ca/Annual_Reports/2024_2025/pdf/agency/bchydro.pdf), and the [2025/26 financial statements](https://www2.gov.bc.ca/assets/gov/british-columbians-our-governments/government-finances/public-accounts/2025-26/sup-e/bc-hydro-fs-2025-26.pdf).

## BC load and generation
- **Internal load**: BC Hydro balancing-authority (BCHA) hourly *Control Area Load* (gross telemetered MW, hour-ending, BC time) from
  [Balancing Authority Load Data](https://www.bchydro.com/energy-in-bc/operations/transmission/transmission-system/balancing-authority-load-data.html):
  `CurrentHourlyBALoad.xls` (current month, posted each business day for the previous day) and yearly `BalancingAuthorityLoad YYYY.xls`.
  Cached in `docs/load_hourly.csv`; each 15-min run only HEAD-checks the files (current at most hourly, yearly every 6 h) and downloads on change.
  This is the whole BCHA area (BC's interconnected grid), wider than BC Hydro's own customers: record 12,334 MW on 2024-01-12 HE18 vs the 11,300 MW BC Hydro cited for customer demand.
- **Generation** is derived: load + net exports on the US and AB ties, so load = generation + net imports holds by construction.
  Independent check: Statistics Canada table 25-10-0015-01 (BC monthly generation, all producers; `docs/statcan_bc_gen.csv`, refreshed daily).
- Peaks are highest **hourly averages**. No public instantaneous/5-min BC load or generation exists (the 5-min page has tie flows only), and EIA-930 has no BCHA demand/generation.
- `python fetch.py --load-history` (one-time) writes annual summaries for 2002-2024 to `docs/load_annual.csv` for record context.
- Load is not valued in C$; it appears on the cards and as a line in the Volume (GWh) chart in the All interties view only.


## Reservoirs

The dashboard's teacup map and **Reservoirs** section show pool elevation, percent of usable storage, 7-day change, and an estimated stored energy (GWh) for the Peace cascade (Williston, Dinosaur, Site C / Naache Mege), the Columbia River Treaty projects in BC (Mica / Kinbasket, Arrow, Duncan), and IJC-regulated Kootenay Lake (Corra Linn, FortisBC). This is not the whole BC Hydro system: Bridge-Coastal, Campbell River, Stave and other Lower Mainland and Island plants are omitted.

- **Schedule:** the same 15-minute workflow (`.github/workflows/update.yml`, cron minutes 7, 22, 37, 52 and `workflow_dispatch`) runs `scripts/fetch_reservoirs.py` before `fetch.py`. A failing reservoir source is logged and skipped (`continue-on-error: true`); it must not block the rest of the data update.
- **Sources (public, no API key):**
  - BC Hydro hydromet text files, ~5-minute, about eight days: `https://www.bchydro.com/info/res_hydromet/data/{code}.txt` — `wsc` Williston (Schooler Creek / WSC 07EF003), `dir` Dinosaur / Peace Canyon, `cwy` (primary) and `cfc` (backup) Site C / Naache Mege, `grt` Kinbasket (WSC 08NB017, used to confirm the Corps Mica forebay). The BC Hydro WAF blocks a bare `curl`; the script sends a browser User-Agent and a hydromet Referer.
  - USACE CWMS Data API, office NWDP, no key: `https://cwms-data.usace.army.mil/cwms-data` — Mica (MCDB) elevation and outflow, Arrow (ARDB) elevation / storage / outflow, Duncan (DCDB) elevation and outflow.
  - **Kootenay Lake:** Water Survey of Canada station **08NH064** (Kootenay Lake at Queens Bay), the IJC reference gauge, geodetic (GSC Datum 1928 Adjustment). Live values from `https://api.weather.gc.ca/collections/hydrometric-realtime/items?STATION_NUMBER=08NH064`; history from the wateroffice unit-value CSV `https://wateroffice.ec.gc.ca/services/real_time_data/csv/inline?stations[]=08NH064&parameters[]=46`. No API key. This matches FortisBC's Queen's Bay reading on `https://secure.fortisbc.com/lakelevel/lakes.jsp` (the public FortisBC page iframes that JSP; daily history is embedded as `var levelData`, plus a Nelson reading and a forecast — scraped as a supplement only). Do not use WSC 08NJ158 for the lake: it is "Kootenay Lake Outflow near Corra Linn", assumed datum, not realtime. CWMS `CORB` is Corra Linn forebay, which is not Queens Bay lake level.
- **History:** hydromet files only keep about eight days, so each run appends new hourly points to `docs/reservoirs_history.csv` (deduped, one row per project per hour). The first run also seeds about a year of Corps Columbia data. `docs/reservoirs.json` is the snapshot attached to `dashboard.json` as `reservoirs` (percent, 7-day change, estimated GWh, assumptions, and chart series).
- **Williston typical-for-date:** `docs/williston_doy_median.json` is a static file of day-of-year medians from the Water Survey of Canada station 07EF003 (HYDAT daily means via `api.weather.gc.ca`). The station's LEVEL is an *assumed* gauge datum, not geodetic elevation; a **630.065 m offset**, calibrated on 20 Nov 2026 (wsc.txt 668.859 m at 16:20 LST vs ECCC LEVEL 38.794 m), is applied and documented in that file. Do not use the raw ECCC LEVEL as a pool elevation. The 15-minute job does not rebuild this file (`python scripts/fetch_reservoirs.py --refresh-median` does).
- **Percent and energy (documented in `reservoirs.json` `assumptions`):** percent full is live storage as a share of the official usable range. Peace projects are linear in elevation (no public Williston storage curve). Mica uses official USACE elevation–storage points (2,320 / 2,434 / 2,475 ft). Energy is E ≈ ρ g h V η / 3.6e12 GWh, η = 0.88, with mid-slice head for Williston and Mica (constant assumed tailwater), an assumed 52 m rated head at Site C, and an assumed 22 m head at Arrow. Duncan is storage-only (no GWh). Kootenay Lake has no stored-energy figure (a natural lake; generation is at Corra Linn and the downstream plants). These are engineering estimates, not a dispatch model. Site C is run-of-river-ish: 1.8 m operating range, 165 million m³ (0.13 MAF, 0.4% of Williston).
- **Kootenay Lake IJC band:** percent is of the 6-ft licensed storage in the IJC 1938 Order of Approval, 1739.32–1745.32 ft at Queens Bay. 1739.32 ft is the post-31 March *maximum* target, not a physical empty, so the lake can read slightly below 0% in late winter and well above 100% in freshet. The dashed teacup mark is the Order maximum for that date: 1745.32 ft from 1 September–7 January, then a linear drawdown (1744.00 ft on 31 January, 1742.40 ft at end of February, 1739.32 ft on 31 March). After 31 March the default max is 1739.32 ft; a calculated higher freshet limit is not modelled, and once the peak recedes the control is 1743.32 ft at Nelson until 1 September.
- Licensed Williston live range is 642.04–672.08 m (32 MAF). The Peace Water Use Plan often uses a higher normal minimum of 654.41 m (the snapshot also publishes `pct_wup`).

## Peak events

The **Peak events** section compares trade during BC Hydro peak events (e.g. Peak Saver "reduce your use between 5 and 9 p.m." days) with ordinary days, to help judge whether an event lines up with grid need (high BC load) or with trading (ordinary load, exports and prices up). It does not draw a conclusion.

- **Event list:** `docs/events.json`, edited by hand. Each event: `date` (BC calendar day), `start`, `end` (BC clock time, 24-hour `HH:MM` on the hour, end exclusive, `24:00` allowed), `source`, `note`. `start 17:00, end 21:00` = 5–9 p.m. = hour-beginning 17–20 = **hour-ending HE18–HE21**, the hour labels BC Hydro's load files use.
- **To add an event (preferred, as soon as the notice arrives):** run the snapshot script, which adds the event *and* records the conditions visible at that moment. Either:
  - **GitHub (has the AESO key):** Actions → *update* → *Run workflow* on `main`, fill `date`, `start`, `end`, `noticed_at`, `source`. Or from a terminal: `gh workflow run update.yml -R warrenboyle/bc-powerflow --ref main -f date=2026-11-26 -f start=17:00 -f end=21:00 -f noticed_at=2026-11-25T16:30-07:00 -f source="BC Hydro Peak Saver email"`. The run snapshots, refreshes the data and commits `docs/`.
  - **Locally:** `AESO_API_KEY=... python scripts/snapshot_event.py --date 2026-11-26 --start 17:00 --end 21:00 --noticed-at 2026-11-25T16:30-07:00 --source "BC Hydro Peak Saver email"`, then commit `docs/events.json` and `docs/event_snapshots.json`.
  - Rerunning with the same event updates it (no duplicates); a later run (e.g. at the day-of reminder) adds another capture, and the page shows the earliest capture taken at notice time.
- **Or by hand:** append an object to `events` in `docs/events.json` (or edit it on GitHub) and commit to `main`; the next data run picks it up, without a conditions snapshot. A future date shows as "upcoming". Bad rows are logged and skipped, and a broken file never blocks the data update.
- **Baseline method:** the same BC clock hours on the **previous 10 weekdays (Mon–Fri)** that were not BC statutory holidays and not themselves event days (weekend/holiday events: the previous 4 weekend days/holidays). This "prior 10 like days" approach is the usual demand-response baseline: recent enough to match season, daylight and weather, and it leaves out other events. A baseline day counts for a measure only if it has every window hour for that measure, and the number of days used is published. Baseline max load = average of each day's highest window hour.
- **Per event** (`peak_events` in `docs/dashboard.json`), for the event window and the baseline, with event − baseline deltas:
  - BC load: average and max MW, max as % of the highest hourly load in the 365 days before the event day, and the share of those 365 days' hours below the event's peak hour.
  - Per tie (US, AB) and both: average net export MW, net MWh, average hub price (AESO hourly; Mid-C is one daily price with the off-peak factor) and estimated value in C$.
  - Whole event day value vs baseline days (while the day is in progress, against the same hours on baseline days).
  - The event day hour by hour (load, flows, prices, value, plus the baseline's average for each hour) for the chart, with the window shaded.
- **Pending data:** BC Hydro posts hourly load each business day for the previous day(s), so event load shows `pending` (status `pending`/`partial`, with hours available) for a day or more; deltas appear only once every window hour is in. Mid-C is published days late, so the US price for recent events is carried forward (flagged "partly est."). The newest AESO hours may also be carried forward up to 3 hours.
- **Conditions when the event was called** (`docs/event_snapshots.json`, written by `scripts/snapshot_event.py`; shown per event). Every field is `{value, source, as_of}`; a source that fails or has nothing for the hours is `null` with a `reason`. Nothing is estimated.
  - *Alberta price and load forecast:* AESO API `poolprice-api` v1.1 (`forecast_pool_price`, `pool_price`) and `actualforecast-api` v1 (forecast Alberta internal load) with `AESO_API_KEY`; keyless fallback, the ETS *Actual/Forecast* report (`ets.aeso.ca/.../ActualForecastWMRQHReportServlet?contentType=csv`). **AESO only publishes a forecast pool price a few hours ahead**, so at an evening-before notice the event hours usually have no price forecast. Forecast AIL covers about the next day.
  - *Alberta grid condition:* status on aeso.ca's *Understanding Grid Conditions* page (Normal / Watch / Attention / Energy Emergency).
  - *Unused dispatchable capability (rough supply cushion):* Σ(maximum capability − net generation) over non-wind/solar fuel types from Current Supply Demand (API with key, else the keyless ETS CSD report). AESO doesn't publish its supply cushion as a number, so this is labelled derived.
  - *Alberta wind and solar:* AESO 7-day forecasts (`wind_rpt_longterm.csv`, `solar_rpt_longterm.csv`, most-likely MW).
  - *BC temperatures:* Open-Meteo (keyless) hourly 2 m temperature for Vancouver, Victoria, Kelowna and Prince George over the window, plus the minimum. Live: the forecast API (falls back to the previous-runs API's latest run).
  - *BC load and Mid-C:* latest posted BC load day (peak and % of the 365-day peak) and the latest Mid-C delivery day, from this repo's CSVs.
  - *Reservoirs:* pool elevation, % of usable storage, outflow (when published) and estimated GWh for Williston, Dinosaur, Site C, Mica, Arrow, Duncan and Kootenay Lake, taken from `docs/reservoirs_history.csv` at or before the notice time. Older events may lack Peace levels because the hydromet files only retain about eight days until the scheduled job has been appending history. See [Reservoirs](#reservoirs).
  - **Retroactive captures** (run more than 2 h after `--noticed-at`, or with `--retroactive`) are flagged and use only kept history: Open-Meteo's previous-runs API `temperature_2m_previous_day1` (the forecast issued about 24 h before each hour, the closest kept forecast to an evening-before notice) plus the day-0 value as "actual ≈"; AESO history if reachable (its kept forecast is the final one made shortly before each hour, not the one at notice time); and load/Mid-C data dated before the notice (posting times aren't tracked, so it may not have been public yet). Current-only sources (grid condition, supply/demand, wind/solar forecast) are `null` for retroactive captures.
- **Similar days without an event** (`similar_days`): non-event days in the 45 days before the event and since, where the event hours had a 4-city average temperature no more than 1 °C warmer than the event's and an average Alberta pool price at least 80% of the event's (a price average needs at least half the window hours settled). Temperatures come from Open-Meteo's historical-forecast API (`docs/bc_temps.csv`, refreshed at most every 6 h; each hour is the model's first-hours value, a stand-in for observed). This shows correlation, not intent.
- Same caveats as the rest of the site: market-price estimates, not Powerex settlements; the US tie has one Mid-C price per day, so hour-to-hour timing inside an event only means something on the Alberta tie.

## Data quality

- `docs/source_compare.csv` keeps every source's MW per hour (`us_bch`, `us_eia`, `us_bch5_raw` (uncorrected real-time page), `ab_bch`, `ab_bch5`, `ab_aeso`), not just the source that wins in `hourly.csv`. `source_qa` in `dashboard.json` publishes bias (mean/median), mean absolute error and correlation for each overlapping pair, e.g. EIA vs BC Hydro official on the US tie. When BC Hydro posts a new month's file, the comparison updates automatically.
- Each period in `dashboard.json` has `completeness`: calendar hours in the period (up to the current hour), valued hours per tie, `missing_to_through` (holes before the tie's latest data hour) and `lag_hours` (hours not yet published). The page footer shows a one-line summary.
- `python fetch.py --seed-audit` re-fills `source_compare.csv` with EIA and BC Hydro official data from 2025-01-01 (one-time).

## Update schedule and reliability

The workflow (`.github/workflows/update.yml`) has a `schedule` cron (minutes 7, 22, 37, 52) and a `workflow_dispatch` trigger. GitHub's scheduler is best-effort: on this repo only 2 of ~61 scheduled 15-minute slots actually ran between 2026-11-01 23:26 UTC and 2026-11-02 14:40 UTC (none failed; the rest were never started). The page therefore shows how old the data is and turns amber ("update delayed") when the last run is over 50 minutes old, and a **Data through** table lists the latest timestamp from each source (`data_through` in `dashboard.json`).

### Optional: reliable external trigger (cron-job.org or similar)

1. Create a **fine-grained personal access token** on GitHub (Settings → Developer settings → Fine-grained tokens): repository access *Only select repositories* → `warrenboyle/bc-powerflow`; permissions **Actions: Read and write** (Metadata: read is added automatically). Nothing else. Set an expiry and note it.
2. On cron-job.org (free), create a job every 15 minutes (e.g. minutes 7,22,37,52):
   - URL: `https://api.github.com/repos/warrenboyle/bc-powerflow/actions/workflows/update.yml/dispatches`
   - Method: `POST`
   - Headers: `Authorization: Bearer <token>`, `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28`, `Content-Type: application/json`
   - Body: `{"ref":"main"}`
   - Success is HTTP `204`.
3. The workflow's `concurrency` group stops overlapping runs, and a run with no data change makes no commit, so the GitHub cron and the external trigger can both stay on.

Equivalent curl: `curl -X POST -H "Authorization: Bearer $TOKEN" -H "Accept: application/vnd.github+json" https://api.github.com/repos/warrenboyle/bc-powerflow/actions/workflows/update.yml/dispatches -d '{"ref":"main"}'`
