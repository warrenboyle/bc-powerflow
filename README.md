# bc-powerflow

BC Hydro intertie flows (BC–US and BC–Alberta) with an estimated wholesale dollar value at hub prices: Mid-C (EIA/ICE daily) for the US tie and the AESO hourly pool price for the Alberta tie.

Dollar figures are market-price estimates (MW × hours × hub price), not BC Hydro/Powerex settlements.

Repo secrets used by the workflow (runs every 15 minutes; source data lags): `EIA_API_KEY` (optional; falls back to DEMO_KEY), `AESO_API_KEY` (enables Alberta data).

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
- **BC Hydro reported figures – different measures** (`reported_context`): static BC Hydro figures for FY2025 and FY2026 (imports, exports, net cost, consolidated trade revenue, and Powerex trade-income ranges), with source links, beside this site's FY2026 numbers. These are **different measures, not like-for-like**: tie-line flows include Powerex trading and other non-purchase flows valued at hub prices, while BC Hydro's reported trade is its domestic purchases and sales at Powerex transfer prices. The price ratios in `different_measures_fy2026` are descriptive only.
  - Sources: [BC Hydro 2025/26 Annual Service Plan Report](https://bcbudget.gov.bc.ca/Annual_Reports/2025_2026/pdf/agency/bchydro.pdf), [2024/25 report](https://bcbudget.gov.bc.ca/Annual_Reports/2024_2025/pdf/agency/bchydro.pdf), and the [2025/26 financial statements](https://www2.gov.bc.ca/assets/gov/british-columbians-our-governments/government-finances/public-accounts/2025-26/sup-e/bc-hydro-fs-2025-26.pdf).

## Data quality

- `docs/source_compare.csv` keeps every source's MW per hour (`us_bch`, `us_eia`, `us_bch5_raw` (uncorrected real-time page), `ab_bch`, `ab_bch5`, `ab_aeso`), not just the source that wins in `hourly.csv`. `source_qa` in `dashboard.json` publishes bias (mean/median), mean absolute error and correlation for each overlapping pair, e.g. EIA vs BC Hydro official on the US tie. When BC Hydro posts a new month's file, the comparison updates automatically.
- Each period in `dashboard.json` has `completeness`: calendar hours in the period (up to the current hour), valued hours per tie, `missing_to_through` (holes before the tie's latest data hour) and `lag_hours` (hours not yet published). The page footer shows a one-line summary.
- `python fetch.py --seed-audit` re-fills `source_compare.csv` with EIA and BC Hydro official data from 2025-01-01 (one-time).
