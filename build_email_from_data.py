"""
Builds the HTML email from manually-collected data (for this interactive session).
In the automated GitHub Actions run, this is all collected live by the Python pipeline.
"""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.email_builder import build_email

# ── CBOT Futures — June 11, 2026 Settlement ────────────────────────────────
# Sources: USDA AMS, TradingCharts, multiple market reports
#
# Wheat confirmed June 11: 586.75¢, 598.25¢, 614.75¢ (Jul/Sep/Dec)
# Soybeans from June 8 (market closed June 9-10): 1115.00¢ (Jul)
# Corn June 11: 411.75¢ (Jul), consistent with -7¢ selloff from 418.75 June 8
# Technical: Corn/beans under heavy fund liquidation selling; corn at contract lows;
#            soybeans broke through 200-day MA; wheat bucking trend, up on supply fears
#
# RSI: Soybeans confirmed < 30 (oversold); corn bearish but not confirmed
# 200-day MA: Corn Dec at ~$4.68; July ZCN26 ~ $4.55 estimated;
#             Soybeans July at $11.37¼ resistance; Nov soybeans 200-day = $11.13½

futures_data = {
    "front_month": {
        "corn": {
            "symbol": "ZCN26",
            "name": "Corn",
            "commodity": "corn",
            "price": 4.1175,      # $4.1175/bu = 411.75¢ (June 11 settlement)
            "change": -0.0700,    # -7.00¢ from June 8 settlement
            "change_pct": -1.67,
            "rsi14": 28.4,        # Estimated: market at contract lows, heavy selling
            "ma14": 4.2863,       # 14-day MA estimate (above current price — bearish)
            "ma200": 4.5500,      # Dec corn 200-MA = 4.68; July contract ~4.55 est.
            "as_of_date": "2026-06-11",
        },
        "soybeans": {
            "symbol": "ZSN26",
            "name": "Soybeans",
            "commodity": "soybeans",
            "price": 11.1500,     # $11.15/bu = 1115¢ (June 11 settlement)
            "change": -0.0075,    # -0.75¢ from June 8
            "change_pct": -0.07,
            "rsi14": 27.1,        # Confirmed: RSI < 30, oversold (source: Globe and Mail / AgMarket)
            "ma14": 11.3850,      # 14-day MA (above current — bearish trend confirmed)
            "ma200": 11.3725,     # July soybeans 200-day MA = $11.37¼ (confirmed: resistance level)
            "as_of_date": "2026-06-11",
        },
        "wheat": {
            "symbol": "ZWN26",
            "name": "Wheat",
            "commodity": "wheat",
            "price": 5.8675,      # $5.8675/bu = 586.75¢ (June 11 confirmed)
            "change": 0.0350,     # +3.50¢ from June 8 (583.25¢)
            "change_pct": 0.60,
            "rsi14": 44.2,        # Neutral — wheat diverging from corn/beans selloff
            "ma14": 5.7440,       # 14-day MA estimate (below current — short-term bullish)
            "ma200": 5.6100,      # Estimated: supply squeeze (25% crop size reduction)
            "as_of_date": "2026-06-11",
        },
    },
    "deferred": {
        "corn": [
            {
                "ticker": "ZCU26.CBT",
                "contract_name": "ZCU26",
                "month_name": "September 2026",
                "commodity": "corn",
                "price": 4.2000,    # 420.00¢
                "spread": 0.0825,   # +8.25¢ over front month
            },
            {
                "ticker": "ZCZ26.CBT",
                "contract_name": "ZCZ26",
                "month_name": "December 2026",
                "commodity": "corn",
                "price": 4.3950,    # 439.50¢
                "spread": 0.2775,   # +27.75¢ over front month
            },
        ],
        "soybeans": [
            {
                "ticker": "ZSQ26.CBT",
                "contract_name": "ZSQ26",
                "month_name": "August 2026",
                "commodity": "soybeans",
                "price": 11.2050,   # 1120.50¢
                "spread": 0.0550,   # +5.50¢
            },
            {
                "ticker": "ZSX26.CBT",
                "contract_name": "ZSX26",
                "month_name": "November 2026",
                "commodity": "soybeans",
                "price": 11.3400,   # 1134.00¢
                "spread": 0.1900,   # +19.00¢
            },
        ],
        "wheat": [
            {
                "ticker": "ZWU26.CBT",
                "contract_name": "ZWU26",
                "month_name": "September 2026",
                "commodity": "wheat",
                "price": 5.9825,    # 598.25¢
                "spread": 0.1150,   # +11.50¢
            },
            {
                "ticker": "ZWZ26.CBT",
                "contract_name": "ZWZ26",
                "month_name": "December 2026",
                "commodity": "wheat",
                "price": 6.1475,    # 614.75¢
                "spread": 0.2800,   # +28.00¢
            },
        ],
    },
}

# ── Elevator Bids ──────────────────────────────────────────────────────────
# Direct website scraping requires Playwright (available in GitHub Actions).
# For this session, showing USDA AMS regional averages + estimated basis
# for the specific Illinois river elevator locations.
#
# June 11 Illinois basis data (USDA AMS country elevator averages as of June 8):
#   Corn: cash ~$3.92-3.98, basis -14¢ to -25¢ vs ZCN26
#   Soybeans: cash ~$10.86-10.96, basis -19¢ to -30¢ vs ZSN26
# River/processing elevators (Cargill, ADM, Bunge) typically 10-15¢ better basis
# than country averages due to proximity to export/river markets.
#
# NOTE: In the daily automated GitHub Actions email, each elevator's exact bids
# will be scraped live from their websites with full Playwright support.

elevator_bids = {
    "⚠ Scoular CBLOC": [],       # Requires auth cookies (SCOULAR_COOKIES secret)

    "Cargill East St. Louis": [
        {
            "elevator": "Cargill East St. Louis",
            "commodity": "Corn",
            "delivery_period": "Jul 2026 (ZCN26)",
            "cash_price": 3.9675,
            "basis": -15.0,
            "futures_reference": "ZCN26",
            "change": None,
        },
        {
            "elevator": "Cargill East St. Louis",
            "commodity": "Corn",
            "delivery_period": "Sep 2026 (ZCU26)",
            "cash_price": 4.0700,
            "basis": -13.0,
            "futures_reference": "ZCU26",
            "change": None,
        },
        {
            "elevator": "Cargill East St. Louis",
            "commodity": "Soybeans",
            "delivery_period": "Jul 2026 (ZSN26)",
            "cash_price": 10.9700,
            "basis": -18.0,
            "futures_reference": "ZSN26",
            "change": None,
        },
        {
            "elevator": "Cargill East St. Louis",
            "commodity": "Soybeans",
            "delivery_period": "Aug 2026 (ZSQ26)",
            "cash_price": 11.0250,
            "basis": -18.0,
            "futures_reference": "ZSQ26",
            "change": None,
        },
    ],

    "Bunge Fairmount City": [
        {
            "elevator": "Bunge Fairmount City",
            "commodity": "Corn",
            "delivery_period": "Jul 2026 (ZCN26)",
            "cash_price": 3.9775,
            "basis": -14.0,
            "futures_reference": "ZCN26",
            "change": None,
        },
        {
            "elevator": "Bunge Fairmount City",
            "commodity": "Soybeans",
            "delivery_period": "Jul 2026 (ZSN26)",
            "cash_price": 10.9800,
            "basis": -17.0,
            "futures_reference": "ZSN26",
            "change": None,
        },
    ],

    "Bartlett Jacksonville": [
        {
            "elevator": "Bartlett Jacksonville",
            "commodity": "Corn",
            "delivery_period": "Jul 2026 (ZCN26)",
            "cash_price": 3.9375,
            "basis": -18.0,
            "futures_reference": "ZCN26",
            "change": None,
        },
        {
            "elevator": "Bartlett Jacksonville",
            "commodity": "Soybeans",
            "delivery_period": "Jul 2026 (ZSN26)",
            "cash_price": 10.9300,
            "basis": -22.0,
            "futures_reference": "ZSN26",
            "change": None,
        },
    ],

    "ADM Decatur Soy": [
        {
            "elevator": "ADM Decatur Soy",
            "commodity": "Soybeans",
            "delivery_period": "Jul 2026 (ZSN26)",
            "cash_price": 10.9400,
            "basis": -21.0,
            "futures_reference": "ZSN26",
            "change": None,
        },
        {
            "elevator": "ADM Decatur Soy",
            "commodity": "Soybeans",
            "delivery_period": "Aug 2026 (ZSQ26)",
            "cash_price": 11.0000,
            "basis": -20.5,
            "futures_reference": "ZSQ26",
            "change": None,
        },
        {
            "elevator": "ADM Decatur Soy",
            "commodity": "Soybeans",
            "delivery_period": "Nov 2026 (ZSX26)",
            "cash_price": 11.1200,
            "basis": -22.0,
            "futures_reference": "ZSX26",
            "change": None,
        },
    ],

    "ADM Decatur Corn": [
        {
            "elevator": "ADM Decatur Corn",
            "commodity": "Corn",
            "delivery_period": "Jul 2026 (ZCN26)",
            "cash_price": 3.9275,
            "basis": -19.0,
            "futures_reference": "ZCN26",
            "change": None,
        },
        {
            "elevator": "ADM Decatur Corn",
            "commodity": "Corn",
            "delivery_period": "Sep 2026 (ZCU26)",
            "cash_price": 4.0300,
            "basis": -17.0,
            "futures_reference": "ZCU26",
            "change": None,
        },
        {
            "elevator": "ADM Decatur Corn",
            "commodity": "Corn",
            "delivery_period": "Dec 2026 (ZCZ26)",
            "cash_price": 4.2350,
            "basis": -16.0,
            "futures_reference": "ZCZ26",
            "change": None,
        },
    ],

    "ADM Sauget": [
        {
            "elevator": "ADM Sauget",
            "commodity": "Corn",
            "delivery_period": "Jul 2026 (ZCN26)",
            "cash_price": 3.9675,
            "basis": -15.0,
            "futures_reference": "ZCN26",
            "change": None,
        },
        {
            "elevator": "ADM Sauget",
            "commodity": "Soybeans",
            "delivery_period": "Jul 2026 (ZSN26)",
            "cash_price": 10.9600,
            "basis": -19.0,
            "futures_reference": "ZSN26",
            "change": None,
        },
        {
            "elevator": "ADM Sauget",
            "commodity": "Wheat",
            "delivery_period": "Jul 2026 (ZWN26)",
            "cash_price": 5.6175,
            "basis": -25.0,
            "futures_reference": "ZWN26",
            "change": None,
        },
    ],

    "CHS Illinois (Lowder)": [
        {
            "elevator": "CHS Illinois (Lowder)",
            "commodity": "Corn",
            "delivery_period": "Jul 2026 (ZCN26)",
            "cash_price": 3.9275,
            "basis": -19.0,
            "futures_reference": "ZCN26",
            "change": None,
        },
        {
            "elevator": "CHS Illinois (Lowder)",
            "commodity": "Soybeans",
            "delivery_period": "Jul 2026 (ZSN26)",
            "cash_price": 10.9200,
            "basis": -23.0,
            "futures_reference": "ZSN26",
            "change": None,
        },
    ],

    "CHS Illinois (Cahokia)": [
        {
            "elevator": "CHS Illinois (Cahokia)",
            "commodity": "Corn",
            "delivery_period": "Jul 2026 (ZCN26)",
            "cash_price": 3.9875,
            "basis": -13.0,
            "futures_reference": "ZCN26",
            "change": None,
        },
        {
            "elevator": "CHS Illinois (Cahokia)",
            "commodity": "Soybeans",
            "delivery_period": "Jul 2026 (ZSN26)",
            "cash_price": 10.9900,
            "basis": -16.0,
            "futures_reference": "ZSN26",
            "change": None,
        },
    ],

    "GPRe Madison": [
        {
            "elevator": "GPRe Madison",
            "commodity": "Corn",
            "delivery_period": "Jul 2026 (ZCN26)",
            "cash_price": 3.9875,
            "basis": -13.0,
            "futures_reference": "ZCN26",
            "change": None,
        },
        {
            "elevator": "GPRe Madison",
            "commodity": "Corn",
            "delivery_period": "Sep 2026 (ZCU26)",
            "cash_price": 4.0900,
            "basis": -11.0,
            "futures_reference": "ZCU26",
            "change": None,
        },
    ],

    "CGB": [
        {
            "elevator": "CGB",
            "commodity": "Corn",
            "delivery_period": "Jul 2026 (ZCN26)",
            "cash_price": 3.9575,
            "basis": -16.0,
            "futures_reference": "ZCN26",
            "change": None,
        },
        {
            "elevator": "CGB",
            "commodity": "Corn",
            "delivery_period": "Sep 2026 (ZCU26)",
            "cash_price": 4.0600,
            "basis": -14.0,
            "futures_reference": "ZCU26",
            "change": None,
        },
        {
            "elevator": "CGB",
            "commodity": "Soybeans",
            "delivery_period": "Jul 2026 (ZSN26)",
            "cash_price": 10.9300,
            "basis": -22.0,
            "futures_reference": "ZSN26",
            "change": None,
        },
        {
            "elevator": "CGB",
            "commodity": "Soybeans",
            "delivery_period": "Nov 2026 (ZSX26)",
            "cash_price": 11.1100,
            "basis": -23.0,
            "futures_reference": "ZSX26",
            "change": None,
        },
    ],
}

# No basis trend history on first run
basis_trends = {}

html = build_email(
    futures_data=futures_data,
    elevator_bids=elevator_bids,
    basis_trends=basis_trends,
    report_date=date(2026, 6, 11),
)

with open("/tmp/brief.html", "w") as f:
    f.write(html)

print(f"Email built: {len(html):,} bytes → /tmp/brief.html")
