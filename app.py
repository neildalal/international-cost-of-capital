"""International Cost of Capital — compare a project's cost of equity and WACC in USD
and in the currency of a selected country.

Data
----
* Unlevered industry betas ........ Damodaran (betas.xls)
* BBB (Baa2/BBB) USD spread ....... Damodaran (ratings.html, large non-financial firms)
* Statutory corporate tax rates ... OECD Corporate Tax Statistics (fallback table embedded)
* 10-year government yields ....... Trading Economics (scraped, no API key)

Model (see README for the full derivation; edit `compute()` if your course formulas differ)
-------------------------------------------------------------------------------------------
    beta_L      = beta_U * (1 + (1 - t) * D/E)
    Ke_USD      = Rf_US + beta_L * ERP
    Kd_USD      = Rf_US + BBB spread
    WACC_USD    = E/V * Ke_USD + D/V * Kd_USD * (1 - t)

    Conversion to the foreign currency (yield-differential / expected-inflation parity):
    FX          = (1 + Rf_FC) / (1 + Rf_US)
    Ke_FC       = (1 + Ke_USD) * FX - 1
    BBB_FC(auto)= (1 + Kd_USD) * FX - 1 - Rf_FC        (or a manual override)
    Kd_FC       = Rf_FC + BBB_FC
    WACC_FC     = E/V * Ke_FC + D/V * Kd_FC * (1 - t)
"""

from __future__ import annotations

import io
import re
import unicodedata
from datetime import datetime, timezone

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
BETAS_URL = "https://pages.stern.nyu.edu/~adamodar/pc/datasets/betas.xls"
RATINGS_URL = "https://pages.stern.nyu.edu/adamodar/New_Home_Page/datafile/ratings.html"
OECD_URL = ("https://www.oecd.org/en/publications/corporate-tax-statistics-2024_9c27d6e8-en/"
            "full-report/statutory-corporate-income-tax-rates_440f9286.html")
TE_URL = "https://tradingeconomics.com/bonds"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}

DEFAULT_INDUSTRY = "Software (System & Application)"
DEFAULT_COUNTRY = "United States"
DEFAULT_ERP = 6.5      # percent
DEFAULT_DEBT = 30      # percent

USD_COLOR = "#2E86C1"
FC_COLOR = "#E67E22"

# Embedded fallback: statutory corporate income tax rates (combined central + sub-central, %),
# OECD Corporate Tax Statistics 2024 (2024 rates). Used only if the live OECD page cannot be parsed.
OECD_FALLBACK_PCT = {
    "Australia": 30, "Austria": 23, "Belgium": 25, "Canada": 26.2, "Chile": 27,
    "Colombia": 35, "Costa Rica": 30, "Czechia": 21, "Denmark": 22, "Estonia": 20,
    "Finland": 20, "France": 25.8, "Germany": 29.9, "Greece": 22, "Hungary": 9,
    "Iceland": 20, "Ireland": 12.5, "Israel": 23, "Italy": 27.8, "Japan": 29.74,
    "Korea": 26.4, "Latvia": 20, "Lithuania": 15, "Luxembourg": 24.94, "Mexico": 30,
    "Netherlands": 25.8, "New Zealand": 28, "Norway": 22, "Poland": 19, "Portugal": 31.5,
    "Slovak Republic": 21, "Slovenia": 22, "Spain": 25, "Sweden": 20.6,
    "Switzerland": 19.6, "Turkiye": 25, "United Kingdom": 25, "United States": 25.77,
    "Argentina": 35, "Brazil": 34, "China": 25, "India": 25.17, "Indonesia": 22,
    "Saudi Arabia": 20, "South Africa": 27, "Russia": 20, "Singapore": 17,
    "Hong Kong": 16.5, "Malaysia": 24, "Thailand": 20, "Vietnam": 20, "Nigeria": 30,
    "Egypt": 22.5, "Philippines": 25, "Peru": 29.5, "Bulgaria": 10, "Croatia": 18,
    "Romania": 16, "Cyprus": 12.5, "Malta": 35, "Ukraine": 18,
}

ALIASES = {
    "south korea": "korea", "republic of korea": "korea", "korea rep": "korea",
    "czech republic": "czechia", "turkey": "turkiye", "slovakia": "slovak republic",
    "russian federation": "russia", "china people s republic of": "china",
    "viet nam": "vietnam", "hong kong sar": "hong kong", "usa": "united states",
}


def norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return ALIASES.get(s, s)


# --------------------------------------------------------------------------- #
# Data fetching (each cached for 1 hour; the Refresh button clears the cache)
# --------------------------------------------------------------------------- #
def _get(url: str) -> requests.Response:
    r = requests.get(url, headers=HEADERS, timeout=40)
    r.raise_for_status()
    return r


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_betas() -> dict:
    """Damodaran industry betas: returns DataFrame[industry, unlevered_beta] + as-of date."""
    r = _get(BETAS_URL)
    sheets = pd.read_excel(io.BytesIO(r.content), sheet_name=None, header=None)

    candidates = []
    for name, raw in sheets.items():
        for i in range(min(60, len(raw))):
            row = raw.iloc[i].astype(str).str.strip().str.lower()
            if (row == "industry name").any():
                candidates.append((name, raw, i))
                break
    if not candidates:
        raise ValueError("Could not find an 'Industry Name' header in betas.xls")
    candidates.sort(key=lambda c: "industry averages" not in c[0].lower())
    _, raw, hdr = candidates[0]

    df = raw.iloc[hdr + 1:].copy()
    df.columns = [str(c).strip() for c in raw.iloc[hdr]]
    lower = {c.lower(): c for c in df.columns}
    ind_col = lower["industry name"]
    beta_col = lower.get("unlevered beta") or next(
        (c for lc, c in lower.items() if lc.startswith("unlevered beta") and "cash" not in lc), None)
    if beta_col is None:
        raise ValueError("Could not find the 'Unlevered beta' column in betas.xls")

    out = pd.DataFrame({
        "industry": df[ind_col].astype(str).str.strip(),
        "unlevered_beta": pd.to_numeric(df[beta_col], errors="coerce"),
    })
    out = out[out["unlevered_beta"].notna() & ~out["industry"].str.lower().str.startswith("total market")]
    out = out[out["industry"].ne("nan")].drop_duplicates("industry").reset_index(drop=True)
    if out.empty:
        raise ValueError("No industry betas parsed from betas.xls")

    head_text = " ".join(raw.iloc[:hdr].astype(str).values.ravel())
    m = re.search(rf"({MONTHS})\s+(\d{{4}})", head_text)
    as_of = f"{m.group(1)} {m.group(2)}" if m else r.headers.get("Last-Modified", "n/a")
    return {"data": out, "as_of": as_of, "fetched_at": _now()}


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_bbb_spread() -> dict:
    """Damodaran synthetic-rating table: Baa2/BBB spread for large non-financial firms."""
    r = _get(RATINGS_URL)
    soup = BeautifulSoup(r.text, "lxml")
    spread = None
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True).replace("\xa0", " ") for c in tr.find_all(["td", "th"])]
        if "Baa2/BBB" in cells:
            for c in cells[cells.index("Baa2/BBB") + 1:]:
                if c.endswith("%"):
                    spread = float(c.rstrip("%")) / 100
                    break
            break
    if spread is None:
        raise ValueError("Baa2/BBB row not found on the Damodaran ratings page")
    m = re.search(r"as of\s+([A-Za-z]+\s+\d{4})", soup.get_text(" "))
    return {"data": spread, "as_of": m.group(1) if m else "n/a", "fetched_at": _now()}


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_tax_rates() -> dict:
    """OECD statutory CIT rates. Tries the live page; falls back to an embedded OECD table."""
    try:
        html = _get(OECD_URL).text
        for t in pd.read_html(io.StringIO(html)):
            first = t.iloc[:, 0].astype(str)
            if first.str.contains("Australia").any() and first.str.contains("United States").any():
                years = [c for c in t.columns if re.fullmatch(r"\d{4}", str(c).strip())]
                if not years:
                    continue
                ycol = sorted(years, key=lambda c: int(str(c).strip()))[-1]
                vals = pd.to_numeric(t[ycol].astype(str).str.extract(r"(\d+(?:\.\d+)?)")[0],
                                     errors="coerce")
                scale = 100 if vals.max() <= 1.0 else 1
                data = {norm(n): v / scale for n, v in zip(first, vals) if pd.notna(v)}
                if len(data) > 20:
                    return {"data": data, "as_of": f"{str(ycol).strip()} rates",
                            "fetched_at": _now(), "fallback": False}
    except Exception:  # noqa: BLE001 - fall through to the embedded table
        pass
    data = {norm(k): v / 100 for k, v in OECD_FALLBACK_PCT.items()}
    return {"data": data, "as_of": "2024 rates (embedded OECD CTS 2024 table)",
            "fetched_at": _now(), "fallback": True}


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_yields() -> dict:
    """Trading Economics 10-year government bond yields (decimal) by country name."""
    r = _get(TE_URL)
    soup = BeautifulSoup(r.text, "lxml")
    yields: dict[str, float] = {}
    dates: dict[str, str] = {}
    for tr in soup.find_all("tr"):
        a = tr.find("a", href=re.compile(r"/[a-z-]+/government-bond-yield$"))
        if not a:
            continue
        country = a.get_text(" ", strip=True)
        cells = [c.get_text(" ", strip=True) for c in tr.find_all("td")]
        value = None
        for c in cells[1:]:
            try:
                value = float(c.replace("%", "").replace(",", ""))
                break
            except ValueError:
                continue
        if country and value is not None and country not in yields:
            yields[country] = value / 100
            dates[country] = cells[-1] if cells else ""
    if not yields:
        raise ValueError("No 10-year yields could be parsed from tradingeconomics.com/bonds")
    us_date = dates.get("United States", "")
    return {"data": yields, "dates": dates, "as_of": us_date or "latest quote",
            "fetched_at": _now()}


def safe(fn):
    try:
        return fn(), None
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #
def compute(beta_u, tax, debt_share, erp, rf_us, rf_fc, spread_us, spread_fc=None) -> dict:
    """Cost of equity, cost of debt and WACC in USD and in the foreign currency.

    All rates are decimals. `beta_u` may be a scalar or a pandas Series.
    If `spread_fc` is None the foreign BBB spread is implied by the yield differential.
    """
    d = debt_share
    e = 1 - d
    beta_l = beta_u * (1 + (1 - tax) * d / e)

    ke_usd = rf_us + beta_l * erp
    kd_usd = rf_us + spread_us
    wacc_usd = e * ke_usd + d * kd_usd * (1 - tax)

    fx = (1 + rf_fc) / (1 + rf_us)
    ke_fc = (1 + ke_usd) * fx - 1
    spread_fc_auto = (1 + kd_usd) * fx - 1 - rf_fc
    spread_fc_used = spread_fc_auto if spread_fc is None else spread_fc
    kd_fc = rf_fc + spread_fc_used
    wacc_fc = e * ke_fc + d * kd_fc * (1 - tax)

    return dict(beta_l=beta_l, ke_usd=ke_usd, kd_usd=kd_usd, kd_usd_at=kd_usd * (1 - tax),
                wacc_usd=wacc_usd, ke_fc=ke_fc, kd_fc=kd_fc, kd_fc_at=kd_fc * (1 - tax),
                wacc_fc=wacc_fc, spread_fc_auto=spread_fc_auto, spread_fc_used=spread_fc_used,
                fx_factor=fx)


# --------------------------------------------------------------------------- #
# UI helpers
# --------------------------------------------------------------------------- #
def pct(x: float | None, nd: int = 2) -> str:
    return "n/a" if x is None else f"{x * 100:.{nd}f}%"


def override_field(label: str, key: str, fetched: float | None, *, percent: bool = True,
                   step: float = 0.01, disabled: bool = False):
    """Fetched value with an optional manual override. Returns (value, is_manual)."""
    forced = fetched is None
    manual = st.toggle(f"Override {label}", value=forced, disabled=forced or disabled,
                       key=f"tg_{key}_{fetched}")
    if manual:
        scale = 100 if percent else 1
        default = None if fetched is None else round(fetched * scale, 4)
        v = st.number_input(label, value=default, step=step, format="%.2f",
                            key=f"in_{key}_{fetched}", label_visibility="collapsed",
                            placeholder="enter value")
        return (None if v is None else v / scale), True
    st.caption(f"Fetched: {pct(fetched) if percent else f'{fetched:.3f}'}")
    return fetched, False


def style_fig(fig: go.Figure, height: int = 380) -> go.Figure:
    fig.update_layout(template="plotly_white", height=height, hovermode="x unified",
                      margin=dict(l=10, r=10, t=50, b=10),
                      legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1))
    fig.update_yaxes(tickformat=".1%", gridcolor="rgba(127,127,127,0.18)")
    return fig


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #
def main() -> None:
    st.set_page_config(page_title="International Cost of Capital", page_icon="💼", layout="wide")
    st.title("💼 International Cost of Capital")
    st.markdown("Compare a project's cost of equity and WACC in **USD** and in the currency of "
                "another country, using the same tax rate, debt share and equity risk premium.")

    # ---- Load data -------------------------------------------------------- #
    betas_r, betas_err = safe(fetch_betas)
    spread_r, spread_err = safe(fetch_bbb_spread)
    tax_r, tax_err = safe(fetch_tax_rates)
    yld_r, yld_err = safe(fetch_yields)

    betas_df = betas_r["data"] if betas_r else pd.DataFrame(columns=["industry", "unlevered_beta"])
    spread_fetched = spread_r["data"] if spread_r else None
    tax_map = tax_r["data"] if tax_r else {}
    yields = yld_r["data"] if yld_r else {}

    industries = list(betas_df["industry"]) or [DEFAULT_INDUSTRY]
    countries = sorted(yields) if yields else sorted(
        {k for k in OECD_FALLBACK_PCT})
    if DEFAULT_COUNTRY not in countries:
        countries = [DEFAULT_COUNTRY] + countries

    # ---- Sidebar: core inputs -------------------------------------------- #
    with st.sidebar:
        st.header("⚙️ Settings")
        if st.button("🔄 Refresh data", width="stretch"):
            st.cache_data.clear()
            st.rerun()

        industry = st.selectbox("Industry", industries,
                                index=industries.index(DEFAULT_INDUSTRY)
                                if DEFAULT_INDUSTRY in industries else 0)
        country = st.selectbox("Target country", countries,
                               index=countries.index(DEFAULT_COUNTRY))
        erp_pct = st.number_input("Equity risk premium (%)", 0.0, 20.0, DEFAULT_ERP, 0.1,
                                  format="%.2f")
        debt_pct = st.slider("Target debt share, D/V (%)", 0, 95, DEFAULT_DEBT, 1)
        tax_choice = st.segmented_control("Project tax rate", ["U.S.", "Foreign country"],
                                          default="U.S.") or "U.S."

        is_us = norm(country) == "united states"

        st.divider()
        with st.expander("✏️ Manual overrides", expanded=False):
            fetched_beta = (float(betas_df.loc[betas_df.industry == industry, "unlevered_beta"].iloc[0])
                            if not betas_df.empty and industry in set(betas_df.industry) else None)
            st.markdown("**Unlevered beta**")
            beta_u, beta_man = override_field("unlevered beta", f"beta_{industry}", fetched_beta,
                                              percent=False)
            st.markdown("**U.S. corporate tax rate**")
            tax_us, tax_us_man = override_field("U.S. tax rate (%)", "tax_us",
                                                tax_map.get("united states"))
            st.markdown("**USD BBB spread**")
            spread_us, spread_us_man = override_field("USD BBB spread (%)", "spread_us",
                                                      spread_fetched)
            st.markdown("**U.S. 10-year yield**")
            rf_us, rf_us_man = override_field("U.S. 10Y yield (%)", "rf_us",
                                              yields.get("United States"))
            fc_slot = st.container()  # foreign BBB spread is rendered here once it can be computed
            if is_us:
                st.info("Target country is the United States, so foreign inputs mirror the U.S. ones.")
                tax_fc, tax_fc_man = tax_us, tax_us_man
                rf_fc, rf_fc_man = rf_us, rf_us_man
            else:
                st.markdown(f"**{country} corporate tax rate**")
                tax_fc, tax_fc_man = override_field(f"{country} tax rate (%)", f"tax_{country}",
                                                    tax_map.get(norm(country)))
                st.markdown(f"**{country} 10-year yield**")
                rf_fc, rf_fc_man = override_field(f"{country} 10Y yield (%)", f"rf_{country}",
                                                  yields.get(country))

    # ---- Resolve inputs ---------------------------------------------------- #
    erp, debt = erp_pct / 100, debt_pct / 100
    tax = tax_us if tax_choice == "U.S." else tax_fc
    missing = [n for n, v in [("unlevered beta", beta_u), ("tax rate (selected regime)", tax),
                              ("USD BBB spread", spread_us), ("U.S. 10Y yield", rf_us),
                              (f"{country} 10Y yield", rf_fc)] if v is None]
    if missing:
        st.warning("Missing inputs: **" + ", ".join(missing) + "**. Open *Manual overrides* in the "
                   "sidebar and enter them (or press Refresh data).")
        render_sources(betas_r, betas_err, spread_r, spread_err, tax_r, tax_err, yld_r, yld_err)
        return

    # Foreign BBB spread: auto (implied by yield differential) unless manually overridden.
    auto = compute(beta_u, tax, debt, erp, rf_us, rf_fc, spread_us)
    fc_spread_manual = False
    fc_spread_val = None
    with fc_slot:
        st.markdown(f"**{'Foreign' if is_us else country} BBB spread**")
        if is_us:
            st.caption("Same as the USD spread.")
        else:
            fc_spread_manual = st.toggle("Override foreign BBB spread", value=False,
                                         key="tg_fc_spread")
            if fc_spread_manual:
                v = st.number_input("Foreign BBB spread (%)",
                                    value=round(auto["spread_fc_auto"] * 100, 4), step=0.01,
                                    format="%.2f", key=f"in_fc_spread_{country}",
                                    label_visibility="collapsed")
                fc_spread_val = (v or 0.0) / 100
            else:
                st.caption(f"Auto-calculated: **{pct(auto['spread_fc_auto'])}** "
                           "(USD spread scaled by the yield differential)")

    res = compute(beta_u, tax, debt, erp, rf_us, rf_fc, spread_us, fc_spread_val)
    fc_name = "U.S. (USD)" if is_us else f"{country}"

    # ---- Header strip -------------------------------------------------------- #
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Industry", industry)
    c2.metric("Project tax rate", pct(tax), "U.S. rate" if tax_choice == "U.S." else f"{country} rate",
              delta_color="off")
    c3.metric("Unlevered → relevered beta", f"{beta_u:.2f} → {res['beta_l']:.2f}")
    c4.metric("Debt share / ERP", f"{debt_pct}% / {erp_pct:.2f}%")

    # ---- Side-by-side results ------------------------------------------------ #
    left, right = st.columns(2)
    with left:
        with st.container(border=True):
            st.subheader("🇺🇸 USD")
            a, b = st.columns(2)
            a.metric("Cost of equity", pct(res["ke_usd"]))
            b.metric("Pre-tax cost of debt", pct(res["kd_usd"]))
            a.metric("After-tax cost of debt", pct(res["kd_usd_at"]))
            b.metric("WACC", pct(res["wacc_usd"]))
    with right:
        with st.container(border=True):
            st.subheader(f"🌍 {fc_name} currency")
            a, b = st.columns(2)
            dlt = lambda x, y: f"{(x - y) * 100:+.2f} pp vs USD"  # noqa: E731
            a.metric("Cost of equity", pct(res["ke_fc"]), dlt(res["ke_fc"], res["ke_usd"]),
                     delta_color="off")
            b.metric("Pre-tax cost of debt", pct(res["kd_fc"]), dlt(res["kd_fc"], res["kd_usd"]),
                     delta_color="off")
            a.metric("After-tax cost of debt", pct(res["kd_fc_at"]),
                     dlt(res["kd_fc_at"], res["kd_usd_at"]), delta_color="off")
            b.metric("WACC", pct(res["wacc_fc"]), dlt(res["wacc_fc"], res["wacc_usd"]),
                     delta_color="off")

    cats = ["Cost of equity", "After-tax cost of debt", "WACC"]
    fig = go.Figure()
    fig.add_bar(name="USD", x=cats, y=[res["ke_usd"], res["kd_usd_at"], res["wacc_usd"]],
                marker_color=USD_COLOR, text=[pct(v) for v in (res["ke_usd"], res["kd_usd_at"], res["wacc_usd"])])
    fig.add_bar(name=f"{fc_name} currency", x=cats,
                y=[res["ke_fc"], res["kd_fc_at"], res["wacc_fc"]], marker_color=FC_COLOR,
                text=[pct(v) for v in (res["ke_fc"], res["kd_fc_at"], res["wacc_fc"])])
    fig.update_layout(barmode="group", title="USD vs foreign currency")
    st.plotly_chart(style_fig(fig, 340), width="stretch")

    # ---- Assumptions --------------------------------------------------------- #
    with st.expander("📘 Main assumptions & formulas", expanded=True):
        st.markdown(
            f"""
- **Same project inputs in both currencies:** one tax rate (**{tax_choice}**, {pct(tax)}), one debt share
  ({debt_pct}%) and one equity risk premium ({erp_pct:.2f}%).
- **Beta:** Damodaran's *unlevered* industry beta is re-levered at the target capital structure.
- **USD:** the U.S. 10-year Treasury yield is the risk-free rate; the cost of debt is the risk-free rate plus the
  Baa2/BBB spread for large non-financial firms.
- **Foreign currency:** USD costs are converted with the yield differential between the two 10-year government
  bonds (an expected-inflation / interest-parity assumption), so risk is the same and only the currency changes.
- **Foreign BBB spread:** by default it is *implied* by that conversion; it can be overridden manually.
- Government yields are quoted rates, not zero-coupon rates; tax rates are statutory, not effective.
"""
        )
        st.latex(r"\beta_L=\beta_U\left[1+(1-t)\frac{D}{E}\right]")
        st.latex(r"K_{e}^{USD}=R_f^{US}+\beta_L\,ERP \qquad K_{d}^{USD}=R_f^{US}+s_{BBB}")
        st.latex(r"WACC=\frac{E}{V}K_e+\frac{D}{V}K_d(1-t)")
        st.latex(r"K^{FC}=(1+K^{USD})\frac{1+R_f^{FC}}{1+R_f^{US}}-1 \qquad "
                 r"s^{FC}_{auto}=(1+K_d^{USD})\frac{1+R_f^{FC}}{1+R_f^{US}}-1-R_f^{FC}")

    # ---- Inputs used --------------------------------------------------------- #
    def src(manual: bool, label: str) -> str:
        return "✏️ Manual" if manual else label

    inputs = pd.DataFrame([
        ("Unlevered beta", f"{beta_u:.3f}", src(beta_man, "Damodaran")),
        ("Relevered beta", f"{res['beta_l']:.3f}", "Calculated"),
        ("Tax rate used", pct(tax),
         src(tax_us_man if tax_choice == "U.S." else tax_fc_man, "OECD")),
        ("U.S. tax rate", pct(tax_us), src(tax_us_man, "OECD")),
        (f"{country} tax rate", pct(tax_fc), src(tax_fc_man, "OECD")),
        ("USD BBB spread", pct(spread_us), src(spread_us_man, "Damodaran")),
        (f"{country} BBB spread", pct(res["spread_fc_used"]),
         "✏️ Manual" if fc_spread_manual else "Calculated (auto)"),
        ("U.S. 10Y yield", pct(rf_us), src(rf_us_man, "Trading Economics")),
        (f"{country} 10Y yield", pct(rf_fc), src(rf_fc_man, "Trading Economics")),
        ("Equity risk premium", pct(erp), "User input"),
        ("Debt share (D/V)", pct(debt), "User input"),
    ], columns=["Input", "Value", "Source"])
    with st.expander("🧾 Inputs used"):
        st.dataframe(inputs, hide_index=True, width="stretch")

    # ---- All-industry comparison ------------------------------------------- #
    st.subheader("All-industry comparison")
    if betas_df.empty:
        st.info("Industry betas are unavailable, so the all-industry comparison cannot be built.")
    else:
        allr = compute(betas_df["unlevered_beta"], tax, debt, erp, rf_us, rf_fc, spread_us,
                       fc_spread_val)
        comp = pd.DataFrame({
            "Industry": betas_df["industry"],
            "Unlevered beta": betas_df["unlevered_beta"],
            "Relevered beta": allr["beta_l"],
            "Ke (USD)": allr["ke_usd"], "WACC (USD)": allr["wacc_usd"],
            "Ke (foreign)": allr["ke_fc"], "WACC (foreign)": allr["wacc_fc"],
        }).sort_values("WACC (USD)").reset_index(drop=True)

        fig2 = go.Figure()
        fig2.add_scatter(x=comp["Industry"], y=comp["WACC (USD)"], name="WACC (USD)",
                         mode="lines+markers", line=dict(color=USD_COLOR))
        fig2.add_scatter(x=comp["Industry"], y=comp["WACC (foreign)"], name=f"WACC ({fc_name})",
                         mode="lines+markers", line=dict(color=FC_COLOR))
        if industry in set(comp.Industry):
            fig2.add_scatter(x=[industry],
                             y=[comp.loc[comp.Industry == industry, "WACC (USD)"].squeeze()],
                             mode="markers", name="Selected",
                             marker=dict(size=14, symbol="star", color="#E74C3C"))
        fig2.update_layout(title="WACC by industry (sorted by USD WACC)", xaxis=dict(showticklabels=False))
        st.plotly_chart(style_fig(fig2, 400), width="stretch")

        fmt = {c: "{:.2%}" for c in comp.columns if c.startswith(("Ke", "WACC"))}
        fmt.update({"Unlevered beta": "{:.3f}", "Relevered beta": "{:.3f}"})
        st.dataframe(comp.style.format(fmt), hide_index=True, width="stretch", height=380)

    # ---- Downloads -------------------------------------------------------------- #
    results_tbl = pd.DataFrame({
        "Metric": ["Cost of equity", "Pre-tax cost of debt", "After-tax cost of debt", "WACC"],
        "USD": [res["ke_usd"], res["kd_usd"], res["kd_usd_at"], res["wacc_usd"]],
        f"{fc_name} currency": [res["ke_fc"], res["kd_fc"], res["kd_fc_at"], res["wacc_fc"]],
    })
    meta = pd.DataFrame({
        "Metric": ["[input] Industry", "[input] Target country", "[input] Tax regime",
                   "[input] Unlevered beta", "[input] Relevered beta", "[input] Tax rate",
                   "[input] ERP", "[input] Debt share", "[input] U.S. 10Y yield",
                   "[input] Foreign 10Y yield", "[input] USD BBB spread", "[input] Foreign BBB spread"],
        "USD": [industry, country, tax_choice, beta_u, res["beta_l"], tax, erp, debt, rf_us, rf_fc,
                spread_us, res["spread_fc_used"]],
        f"{fc_name} currency": [""] * 12,
    })
    export = pd.concat([meta, results_tbl], ignore_index=True)

    d1, d2 = st.columns(2)
    d1.download_button("📥 Download results (CSV)", export.to_csv(index=False).encode("utf-8"),
                       file_name=f"cost_of_capital_{norm(industry).replace(' ', '_')}_{norm(country).replace(' ', '_')}.csv",
                       mime="text/csv", width="stretch")
    if not betas_df.empty:
        d2.download_button("📥 Download all industries (CSV)", comp.to_csv(index=False).encode("utf-8"),
                           file_name=f"cost_of_capital_all_industries_{norm(country).replace(' ', '_')}.csv",
                           mime="text/csv", width="stretch")

    render_sources(betas_r, betas_err, spread_r, spread_err, tax_r, tax_err, yld_r, yld_err)


def render_sources(betas_r, betas_err, spread_r, spread_err, tax_r, tax_err, yld_r, yld_err) -> None:
    def row(name, url, r, err, note=""):
        if err:
            return dict(Source=name, **{"Data date": "—", "Fetched": "—"},
                        Status=f"⚠️ Failed ({err[:80]}) — use manual inputs", Link=url)
        status = "⚠️ Fallback table (live page not parsed)" if r.get("fallback") else "✅ Live"
        return dict(Source=name, **{"Data date": r["as_of"], "Fetched": r["fetched_at"]},
                    Status=(status + (f" — {note}" if note else "")), Link=url)

    rows = [
        row("Damodaran — unlevered industry betas", BETAS_URL, betas_r, betas_err),
        row("Damodaran — Baa2/BBB spread (large non-financial firms)", RATINGS_URL, spread_r, spread_err),
        row("OECD — statutory corporate tax rates", OECD_URL, tax_r, tax_err),
        row("Trading Economics — 10-year government yields", TE_URL, yld_r, yld_err,
            "date shown is the U.S. quote date" if yld_r else ""),
    ]
    st.subheader("Sources & data dates")
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                 column_config={"Link": st.column_config.LinkColumn("Link", display_text="open")})
    st.caption("Use **🔄 Refresh data** in the sidebar to re-fetch all sources. "
               "Educational tool — verify the numbers against your course formulas.")


if __name__ == "__main__":
    main()
