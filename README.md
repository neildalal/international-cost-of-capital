# International Cost of Capital

A Streamlit app that compares a project's **cost of equity, cost of debt and WACC in USD and in the currency of a selected country**, using the latest data from Damodaran, the OECD and Trading Economics.

**Defaults on open:** Software (System & Application), United States as the target country, U.S. tax rate, 6.5% equity risk premium, 30% debt share.

## Features

- Pulls data live, shows each source with its **data date** and **fetch time**, and has a **🔄 Refresh data** button.
- Choose an industry and a target country; adjust the equity risk premium and the debt share.
- Switch the project tax rate between the **U.S.** and the **selected country**.
- **Manual overrides** for the unlevered beta, both tax rates, the USD BBB spread, both 10-year yields and the foreign BBB spread. With the foreign spread override off, the app shows the auto-calculated value.
- Side-by-side USD vs foreign-currency results, a chart, an assumptions panel, an "inputs used" table, an **all-industry comparison**, and **CSV downloads**.

## Data sources

| Input | Source | Detail |
|---|---|---|
| Unlevered industry beta | Damodaran, `betas.xls` | "Unlevered beta" column, US industry averages |
| BBB spread | Damodaran, `ratings.html` | Baa2/BBB, large non-financial service firms |
| Corporate tax rate | OECD Corporate Tax Statistics | Combined (central + sub-central) statutory rate, latest year |
| 10-year government yields | Trading Economics (scraped) | Latest quote per country |

**How the OECD and Trading Economics data are fetched.** Those two sites are the hardest to scrape, so each is tried in layers; the *Sources & data dates* table shows which layer worked and why earlier ones failed.

- **OECD tax rates:** (1) the OECD Data Explorer SDMX API (`DSD_TAX_CIT@DF_CIT`, the data behind the Corporate Tax Statistics tables; the publication page itself loads its table dynamically, so scraping it usually fails) → (2) the publication page → (3) an embedded copy of the OECD 2024 table, labelled as a fallback.
- **10-year yields (scraping only, no API):** the `tradingeconomics.com/bonds` list is filled in by JavaScript, so it cannot be scraped. Instead the app reads Trading Economics' static pages: (1) the 10-year table at `/forecast/government-bond-10y`, which lists 22 major economies (US, UK, Japan, Germany, Brazil, India, Canada, Italy, France, South Africa, China, Switzerland, Mexico, Korea, Spain, Turkey, …) in one request; (2) for any other country, that country's own page (`/<country>/government-bond-yield`), fetched when you select it. The U.S. yield comes from (1), or from the U.S. page if (1) fails.
- If a source fails, the app says so and you can type the value in under *Manual overrides*.

## Model (verify against your course formulas)

The class formulas were not available when this app was generated, so it uses the standard approach below. If your course uses different formulas, edit `compute()` in `app.py`; it is the only place the finance lives.

Let `t` = project tax rate, `D/V` = debt share, `E/V = 1 − D/V`.

```
beta_L   = beta_U × (1 + (1 − t) × D/E)                  D/E = (D/V)/(E/V)

USD
Ke_USD   = Rf_US + beta_L × ERP
Kd_USD   = Rf_US + BBB spread
WACC_USD = E/V × Ke_USD + D/V × Kd_USD × (1 − t)

Foreign currency (yield-differential conversion)
FX       = (1 + Rf_FC) / (1 + Rf_US)
Ke_FC    = (1 + Ke_USD) × FX − 1
BBB_FC   = (1 + Kd_USD) × FX − 1 − Rf_FC                 (auto; can be overridden)
Kd_FC    = Rf_FC + BBB_FC
WACC_FC  = E/V × Ke_FC + D/V × Kd_FC × (1 − t)
```

Assumptions: the same tax rate, debt share and ERP are used in both currencies; the yield differential between the two 10-year government bonds reflects the expected inflation (currency) difference, so the project's risk is unchanged; statutory tax rates and quoted yields are used as given.

### Worked check (hypothetical inputs)

`beta_U = 1.20`, `t = 25%`, `D/V = 30%`, `ERP = 6.5%`, `Rf_US = 4.0%`, BBB spread `= 1.11%`, `Rf_FC = 2.0%`:

| | USD | Foreign |
|---|---|---|
| Relevered beta | 1.5857 | 1.5857 |
| Cost of equity | 14.31% | 12.11% |
| Pre-tax cost of debt | 5.11% | 3.09% (spread 1.09%) |
| After-tax cost of debt | 3.83% | 2.32% |
| WACC | 11.16% | 9.17% |

Sanity checks built into the design: if the target country is the U.S., both columns are identical; if the foreign yield equals the U.S. yield, the foreign costs equal the USD costs.

## Run locally (Python 3.12)

```bash
python3.12 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

## Deploy on Streamlit Community Cloud

1. Create a GitHub repository and upload `app.py`, `requirements.txt` and `README.md` (at the repository root).
2. Go to <https://share.streamlit.io>, click **Create app**, pick the repository and branch, and set the main file to `app.py`.
3. In *Advanced settings* choose Python 3.12, then **Deploy**.

## Limitations

- Trading Economics may block automated requests from some cloud hosts; if so, the sources table shows the error and you can type the yield into *Manual overrides*. Check their terms of use for your purposes.
- Source pages can change layout; the app then shows the reason in the sources table.
- Data dates differ by source: OECD rates are annual, Trading Economics yields are the latest quote (the page's update date is shown), Damodaran data is as of the date on his page.
- The country dropdown lists the major economies plus other countries that normally have a Trading Economics 10-year page; if a country has no page or no OECD tax rate, enter the value manually.
- Data is cached for 30 minutes; use **Refresh data** to re-fetch.
- Educational tool, not investment advice.
