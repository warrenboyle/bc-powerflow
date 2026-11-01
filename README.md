# bc-powerflow

Proof of concept: BC Hydro intertie flows (EIA BPAT-BCHA interchange) with estimated wholesale dollar value at Mid-C (EIA/ICE daily) and, when an AESO key is set, Alberta pool price + BC-AB intertie.

Dollar figures are market-price estimates (MW x hours x hub price), not BC Hydro/Powerex settlements.

Repo secrets used by the hourly workflow: `EIA_API_KEY` (optional; falls back to DEMO_KEY), `AESO_API_KEY` (enables Alberta data).

Site: https://warrenboyle.github.io/bc-powerflow/
