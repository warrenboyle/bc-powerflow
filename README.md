# bc-powerflow

Proof of concept: BC Hydro intertie flows (EIA BPAT-BCHA interchange) with estimated wholesale dollar value at Mid-C (EIA/ICE daily) and, when an AESO key is set, Alberta pool price + BC-AB intertie.

Dollar figures are market-price estimates (MW x hours x hub price), not BC Hydro/Powerex settlements.

Repo secrets used by the workflow (runs every 15 minutes; source data lags): `EIA_API_KEY` (optional; falls back to DEMO_KEY), `AESO_API_KEY` (enables Alberta data).

Site: https://warrenboyle.github.io/bc-powerflow/

## Dashboard sections

- **Rolling totals** (month to date, previous month, year to date, previous year): tie-line flows valued at hub prices (Mid-C day-ahead peak + AESO pool). This is *not* BC Hydro's import bill nor Powerex's trading margin.
- **Estimated arbitrage value** (`arbitrage_estimate` in `docs/dashboard.json`): a separate heuristic stream, labelled ESTIMATE. It is not Powerex's actual arbitrage profit, which is only published as multi-year ranges.
  - *Timing value* = Σ hourly flow × (hourly price − that tie's simple average price for the calendar month), summed over the BC–US and BC–AB ties. It measures value from *when* BC exported or imported within the month, versus moving the same net energy flat.
  - *Round-trip spread value* = (flow-weighted avg export price − flow-weighted avg import price) × min(export MWh, import MWh), per month, both ties pooled.
  - Both are computed per calendar month. Rolling totals are sums of months (`periods`), and there is a `monthly` series.
  - *Off-peak sensitivity*: Mid-C is a daily peak index applied to every hour. The `offpeak_sens` figures price Mid-C off-peak hours (Pacific HE23–HE6 Mon–Sat and all Sunday; holidays not included) at `OFFPEAK_FACTOR` × peak (env var, default 0.8). This is an assumption, not data.
- **Reported context** (`reported_context`): static BC Hydro figures for FY2025 and FY2026 (imports, exports, net cost, consolidated trade revenue, and Powerex trade-income ranges), with source links. It also includes the dashboard's own hub-valued FY2026 numbers, recomputed from `hourly.csv`, and a calibration block. That block gives the reported-vs-hub price ratios and the *hub-to-reported import cost gap* (reported FY import cost − hub-valued import cost), which indicates shaping, wheeling and transfer-pricing costs.
  - Sources: [BC Hydro 2025/26 Annual Service Plan Report](https://bcbudget.gov.bc.ca/Annual_Reports/2025_2026/pdf/agency/bchydro.pdf), [2024/25 report](https://bcbudget.gov.bc.ca/Annual_Reports/2024_2025/pdf/agency/bchydro.pdf), and the [2025/26 financial statements](https://www2.gov.bc.ca/assets/gov/british-columbians-our-governments/government-finances/public-accounts/2025-26/sup-e/bc-hydro-fs-2025-26.pdf).
