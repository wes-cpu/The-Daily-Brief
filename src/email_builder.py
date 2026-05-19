"""
email_builder.py - Builds a clean, professional HTML email for the daily grain brief.

Sections:
  1. Market Overview (summary banner)
  2. Futures Prices (front-month corn, soybeans, wheat)
  3. Deferred Spreads
  4. Technical Analysis (RSI, MA14, MA200)
  5. Local Elevator Cash Bids (table per elevator, all months + basis)
  6. Basis Trends
  7. Warnings for failed elevators
"""

import logging
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger(__name__)

# Brand colors
COLOR_PRIMARY = "#2d5a1b"       # Dark green header
COLOR_LIGHT_GREEN = "#e8f5e2"   # Light green background
COLOR_ACCENT = "#4a8c2e"        # Medium green for accents
COLOR_WARNING = "#d97706"       # Amber for warnings
COLOR_ERROR = "#dc2626"         # Red for errors
COLOR_POSITIVE = "#16a34a"      # Green for positive values
COLOR_NEGATIVE = "#dc2626"      # Red for negative values
COLOR_NEUTRAL = "#374151"       # Dark gray for neutral text
COLOR_TABLE_HEADER = "#374151"
COLOR_TABLE_ROW_ALT = "#f9fafb"
COLOR_BORDER = "#e5e7eb"


def _fmt_price(price: Optional[float], decimals: int = 4) -> str:
    """Format a $/bu futures price (e.g., 4.5025)."""
    if price is None:
        return "N/A"
    return f"${price:.{decimals}f}"


def _fmt_cash(price: Optional[float]) -> str:
    """Format a $/bu cash price (2 decimals)."""
    if price is None:
        return "N/A"
    return f"${price:.2f}"


def _fmt_basis(basis: Optional[float]) -> str:
    """Format basis in ¢/bu with sign (e.g., '+5' or '-20')."""
    if basis is None:
        return "N/A"
    sign = "+" if basis >= 0 else ""
    # Basis is typically already in cents; if it appears to be in dollars convert
    if abs(basis) < 2.0 and basis != 0:
        # Probably dollars, convert to cents
        basis_cents = basis * 100
        return f"{sign}{basis_cents:.0f}¢"
    return f"{sign}{basis:.0f}¢"


def _fmt_change(change: Optional[float], is_pct: bool = False) -> str:
    """Format a daily change value with color hint prefix."""
    if change is None:
        return "N/A"
    if is_pct:
        sign = "+" if change >= 0 else ""
        return f"{sign}{change:.2f}%"
    sign = "+" if change >= 0 else ""
    return f"{sign}{change:.4f}"


def _change_color(change: Optional[float]) -> str:
    if change is None:
        return COLOR_NEUTRAL
    return COLOR_POSITIVE if change >= 0 else COLOR_NEGATIVE


def _trend_badge(trend: str) -> str:
    """Return an HTML badge/span for a trend direction."""
    styles = {
        "strengthening": f'background:#dcfce7;color:#15803d;padding:2px 6px;border-radius:4px;font-size:11px;font-weight:600;',
        "weakening": f'background:#fee2e2;color:#b91c1c;padding:2px 6px;border-radius:4px;font-size:11px;font-weight:600;',
        "unchanged": f'background:#f3f4f6;color:#6b7280;padding:2px 6px;border-radius:4px;font-size:11px;font-weight:600;',
        "N/A": f'background:#f3f4f6;color:#9ca3af;padding:2px 6px;border-radius:4px;font-size:11px;',
    }
    style = styles.get(trend, styles["N/A"])
    labels = {
        "strengthening": "▲ Strengthening",
        "weakening": "▼ Weakening",
        "unchanged": "→ Unchanged",
        "N/A": "N/A",
    }
    label = labels.get(trend, trend)
    return f'<span style="{style}">{label}</span>'


def _section_header(title: str) -> str:
    return f"""
    <tr>
      <td colspan="10" style="padding:20px 0 8px 0;">
        <h2 style="margin:0;font-size:16px;font-weight:700;color:{COLOR_PRIMARY};
                   border-bottom:2px solid {COLOR_PRIMARY};padding-bottom:6px;
                   font-family:Arial,sans-serif;">
          {title}
        </h2>
      </td>
    </tr>"""


def _table_style() -> str:
    return (
        "width:100%;border-collapse:collapse;font-family:Arial,sans-serif;"
        "font-size:13px;margin-bottom:16px;"
    )


def _th_style() -> str:
    return (
        f"background:{COLOR_TABLE_HEADER};color:#fff;padding:8px 10px;"
        "text-align:left;font-weight:600;font-size:12px;"
    )


def _td_style(alt: bool = False) -> str:
    bg = COLOR_TABLE_ROW_ALT if alt else "#ffffff"
    return f"background:{bg};padding:7px 10px;border-bottom:1px solid {COLOR_BORDER};color:{COLOR_NEUTRAL};"


def build_email(
    futures_data: dict,
    elevator_bids: dict[str, list[dict]],
    basis_trends: dict[str, dict],
    elevator_spreads: Optional[dict] = None,
    report_date: Optional[date] = None,
) -> str:
    """
    Build the complete HTML email.

    Args:
        futures_data: return value of fetch_all_futures()
        elevator_bids: keyed by elevator name, list of bid dicts
        basis_trends: keyed by "ElevatorName|Commodity", trend dicts
        report_date: date for the report (defaults to today)

    Returns:
        Full HTML string suitable for sending as an HTML email.
    """
    if report_date is None:
        report_date = date.today()

    weekday = report_date.strftime("%A")
    date_str = report_date.strftime("%B %d, %Y")
    generated_at = datetime.now().strftime("%I:%M %p CT")

    front_month = futures_data.get("front_month", {})
    deferred = futures_data.get("deferred", {})

    # Determine failed elevators
    failed_elevators = [name for name, bids in elevator_bids.items() if not bids]
    successful_elevators = [name for name, bids in elevator_bids.items() if bids]

    # -----------------------------------------------------------------------
    # Build each section
    # -----------------------------------------------------------------------

    html_market_overview = _build_market_overview(front_month, date_str, weekday)
    html_futures = _build_futures_table(front_month)
    html_deferred = _build_deferred_table(deferred)
    html_technicals = _build_technicals_table(front_month)
    html_elevator_bids = _build_elevator_bids_section(elevator_bids)
    html_basis_trends = _build_basis_trends_section(basis_trends, elevator_bids)
    html_elevator_spreads = _build_elevator_spreads_section(elevator_spreads or {})
    html_warnings = _build_warnings(failed_elevators)

    # -----------------------------------------------------------------------
    # Assemble full email
    # -----------------------------------------------------------------------

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Daily Grain Brief | {weekday}, {date_str}</title>
</head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:Arial,Helvetica,sans-serif;">

<!-- Outer wrapper -->
<table width="100%" cellpadding="0" cellspacing="0"
       style="background:#f3f4f6;padding:20px 0;">
  <tr>
    <td align="center">

<!-- Main container -->
<table width="680" cellpadding="0" cellspacing="0"
       style="background:#ffffff;border-radius:8px;overflow:hidden;
              box-shadow:0 2px 8px rgba(0,0,0,0.1);max-width:680px;">

  <!-- Header -->
  <tr>
    <td style="background:{COLOR_PRIMARY};padding:24px 28px;">
      <table width="100%">
        <tr>
          <td>
            <div style="color:#ffffff;font-size:22px;font-weight:700;
                        font-family:Arial,sans-serif;letter-spacing:-0.3px;">
              🌽 The Daily Grain Brief
            </div>
            <div style="color:#a8d08d;font-size:13px;margin-top:4px;
                        font-family:Arial,sans-serif;">
              {weekday}, {date_str} &nbsp;·&nbsp; Generated {generated_at}
            </div>
          </td>
          <td align="right" style="vertical-align:top;">
            <div style="background:rgba(255,255,255,0.15);border-radius:6px;
                        padding:8px 14px;color:#fff;font-size:12px;
                        font-family:Arial,sans-serif;">
              CBOT Settlement
            </div>
          </td>
        </tr>
      </table>
    </td>
  </tr>

  <!-- Body content -->
  <tr>
    <td style="padding:24px 28px;">

      {html_warnings}

      <!-- Market Overview Banner -->
      {html_market_overview}

      <!-- Futures Prices -->
      <h2 style="font-size:15px;font-weight:700;color:{COLOR_PRIMARY};
                 border-bottom:2px solid {COLOR_PRIMARY};padding-bottom:6px;
                 margin:20px 0 12px 0;font-family:Arial,sans-serif;">
        Futures Prices (Front Month — Continuous)
      </h2>
      {html_futures}

      <!-- Deferred Spreads -->
      <h2 style="font-size:15px;font-weight:700;color:{COLOR_PRIMARY};
                 border-bottom:2px solid {COLOR_PRIMARY};padding-bottom:6px;
                 margin:24px 0 12px 0;font-family:Arial,sans-serif;">
        Deferred Contract Spreads
      </h2>
      {html_deferred}

      <!-- Technical Analysis -->
      <h2 style="font-size:15px;font-weight:700;color:{COLOR_PRIMARY};
                 border-bottom:2px solid {COLOR_PRIMARY};padding-bottom:6px;
                 margin:24px 0 12px 0;font-family:Arial,sans-serif;">
        Technical Analysis
      </h2>
      {html_technicals}

      <!-- Elevator Cash Bids -->
      <h2 style="font-size:15px;font-weight:700;color:{COLOR_PRIMARY};
                 border-bottom:2px solid {COLOR_PRIMARY};padding-bottom:6px;
                 margin:24px 0 12px 0;font-family:Arial,sans-serif;">
        Local Elevator Cash Bids
      </h2>
      {html_elevator_bids}

      <!-- Basis Trends -->
      <h2 style="font-size:15px;font-weight:700;color:{COLOR_PRIMARY};
                 border-bottom:2px solid {COLOR_PRIMARY};padding-bottom:6px;
                 margin:24px 0 12px 0;font-family:Arial,sans-serif;">
        Basis Trends
      </h2>
      {html_basis_trends}

      <!-- Elevator Cash Bid Deferred Spreads -->
      <h2 style="font-size:15px;font-weight:700;color:{COLOR_PRIMARY};
                 border-bottom:2px solid {COLOR_PRIMARY};padding-bottom:6px;
                 margin:24px 0 12px 0;font-family:Arial,sans-serif;">
        Elevator Deferred Spread vs. Front Month
      </h2>
      {html_elevator_spreads}

    </td>
  </tr>

  <!-- Footer -->
  <tr>
    <td style="background:{COLOR_LIGHT_GREEN};padding:16px 28px;
               border-top:1px solid {COLOR_BORDER};">
      <div style="color:#6b7280;font-size:11px;font-family:Arial,sans-serif;
                  text-align:center;line-height:1.5;">
        The Daily Grain Brief &nbsp;·&nbsp; Automated market intelligence
        &nbsp;·&nbsp; Data from CBOT settlement &amp; elevator websites<br>
        Prices are informational only. Not financial advice.
        Verify all bids directly with elevators before making marketing decisions.
      </div>
    </td>
  </tr>

</table>
<!-- /Main container -->

    </td>
  </tr>
</table>
<!-- /Outer wrapper -->

</body>
</html>"""

    return html


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

def _build_market_overview(front_month: dict, date_str: str, weekday: str) -> str:
    """Build the top summary banner with key prices."""
    items = []
    commodity_emojis = {"corn": "🌽", "soybeans": "🫘", "wheat": "🌾"}
    for commodity in ["corn", "soybeans", "wheat"]:
        data = front_month.get(commodity)
        if not data:
            continue
        emoji = commodity_emojis.get(commodity, "")
        price = _fmt_price(data.get("price"), 4)
        change = data.get("change")
        change_str = _fmt_change(change)
        change_color = _change_color(change)
        change_pct = data.get("change_pct")
        pct_str = f"({_fmt_change(change_pct, is_pct=True)})" if change_pct is not None else ""
        items.append(f"""
          <td style="padding:12px 16px;border-right:1px solid rgba(255,255,255,0.2);
                     text-align:center;vertical-align:top;">
            <div style="font-size:11px;color:#a8d08d;font-family:Arial,sans-serif;
                        text-transform:uppercase;letter-spacing:0.5px;">
              {emoji} {data.get('name', commodity.title())}
            </div>
            <div style="font-size:20px;font-weight:700;color:#ffffff;
                        font-family:Arial,sans-serif;margin-top:2px;">
              {price}
            </div>
            <div style="font-size:12px;color:{change_color};font-family:Arial,sans-serif;
                        margin-top:2px;">
              {change_str} {pct_str}
            </div>
          </td>""")

    if not items:
        return ""

    return f"""
    <table width="100%" cellpadding="0" cellspacing="0"
           style="background:{COLOR_PRIMARY};border-radius:6px;margin-bottom:8px;">
      <tr>
        {"".join(items)}
      </tr>
    </table>"""


def _build_futures_table(front_month: dict) -> str:
    """Build the front-month futures price table."""
    rows_html = ""
    for i, commodity in enumerate(["corn", "soybeans", "wheat"]):
        data = front_month.get(commodity)
        if not data:
            rows_html += f"""
        <tr>
          <td style="{_td_style(i % 2 == 1)}">{commodity.title()}</td>
          <td style="{_td_style(i % 2 == 1)}" colspan="5">Data unavailable</td>
        </tr>"""
            continue

        price = _fmt_price(data.get("price"), 4)
        change = data.get("change")
        change_color = _change_color(change)
        change_str = _fmt_change(change)
        change_pct = data.get("change_pct")
        pct_str = _fmt_change(change_pct, is_pct=True)
        symbol = data.get("symbol", "")
        as_of = data.get("as_of_date", "")

        alt = i % 2 == 1
        rows_html += f"""
        <tr>
          <td style="{_td_style(alt)}"><strong>{data.get('name', commodity.title())}</strong></td>
          <td style="{_td_style(alt)};font-family:monospace;">{symbol}</td>
          <td style="{_td_style(alt)};font-weight:600;">{price}</td>
          <td style="{_td_style(alt)};color:{change_color};font-weight:600;">{change_str}</td>
          <td style="{_td_style(alt)};color:{change_color};">{pct_str}</td>
          <td style="{_td_style(alt)};color:#9ca3af;font-size:11px;">{as_of}</td>
        </tr>"""

    return f"""
    <table style="{_table_style()}">
      <thead>
        <tr>
          <th style="{_th_style()}">Commodity</th>
          <th style="{_th_style()}">Symbol</th>
          <th style="{_th_style()}">Price ($/bu)</th>
          <th style="{_th_style()}">Change</th>
          <th style="{_th_style()}">% Change</th>
          <th style="{_th_style()}">As Of</th>
        </tr>
      </thead>
      <tbody>
        {rows_html}
      </tbody>
    </table>"""


def _build_deferred_table(deferred: dict) -> str:
    """Build the deferred contracts spread table."""
    rows_html = ""
    row_idx = 0
    commodity_emojis = {"corn": "🌽", "soybeans": "🫘", "wheat": "🌾"}

    for commodity in ["corn", "soybeans", "wheat"]:
        contracts = deferred.get(commodity, [])
        if not contracts:
            alt = row_idx % 2 == 1
            rows_html += f"""
        <tr>
          <td style="{_td_style(alt)}">{commodity_emojis.get(commodity,'')} {commodity.title()}</td>
          <td style="{_td_style(alt)}" colspan="4">No deferred data</td>
        </tr>"""
            row_idx += 1
            continue

        for contract in contracts:
            alt = row_idx % 2 == 1
            spread = contract.get("spread")
            spread_color = _change_color(spread)
            spread_str = _fmt_change(spread) if spread is not None else "N/A"
            price = _fmt_price(contract.get("price"), 4)

            spread_change = contract.get("spread_change")
            if spread_change is None:
                sc_str = "<span style='color:#9ca3af;font-size:11px;'>no prior data</span>"
            else:
                sc_color = _change_color(spread_change)
                arrow = "▲" if spread_change > 0 else ("▼" if spread_change < 0 else "→")
                sc_str = f'<span style="color:{sc_color};font-weight:600;">{arrow} {_fmt_change(spread_change)}</span>'

            rows_html += f"""
        <tr>
          <td style="{_td_style(alt)}">{commodity_emojis.get(commodity,'')} {commodity.title()}</td>
          <td style="{_td_style(alt)};font-family:monospace;">{contract.get('contract_name','')}</td>
          <td style="{_td_style(alt)};">{contract.get('month_name','')}</td>
          <td style="{_td_style(alt)};font-weight:600;">{price}</td>
          <td style="{_td_style(alt)};color:{spread_color};font-weight:600;">{spread_str}</td>
          <td style="{_td_style(alt)};">{sc_str}</td>
        </tr>"""
            row_idx += 1

    if not rows_html:
        return "<p style='color:#9ca3af;font-size:13px;'>No deferred contract data available.</p>"

    return f"""
    <table style="{_table_style()}">
      <thead>
        <tr>
          <th style="{_th_style()}">Commodity</th>
          <th style="{_th_style()}">Contract</th>
          <th style="{_th_style()}">Month</th>
          <th style="{_th_style()}">Price ($/bu)</th>
          <th style="{_th_style()}">Spread vs Front</th>
          <th style="{_th_style()}">Spread Change</th>
        </tr>
      </thead>
      <tbody>
        {rows_html}
      </tbody>
    </table>"""


def _build_technicals_table(front_month: dict) -> str:
    """Build the technical indicators table (RSI, MA14, MA200)."""
    rows_html = ""
    commodity_emojis = {"corn": "🌽", "soybeans": "🫘", "wheat": "🌾"}

    for i, commodity in enumerate(["corn", "soybeans", "wheat"]):
        data = front_month.get(commodity)
        alt = i % 2 == 1

        if not data:
            rows_html += f"""
        <tr>
          <td style="{_td_style(alt)}">{commodity.title()}</td>
          <td style="{_td_style(alt)}" colspan="4">Data unavailable</td>
        </tr>"""
            continue

        price = data.get("price")
        rsi14 = data.get("rsi14")
        ma14 = data.get("ma14")
        ma200 = data.get("ma200")

        # RSI interpretation
        rsi_color = COLOR_NEUTRAL
        rsi_label = ""
        if rsi14 is not None:
            if rsi14 >= 70:
                rsi_color = COLOR_NEGATIVE
                rsi_label = " (Overbought)"
            elif rsi14 <= 30:
                rsi_color = COLOR_POSITIVE
                rsi_label = " (Oversold)"

        # Price vs MA200
        vs_ma200 = ""
        if price is not None and ma200 is not None:
            if price > ma200:
                vs_ma200 = f'<span style="color:{COLOR_POSITIVE};font-size:11px;"> ▲ Above 200-MA</span>'
            else:
                vs_ma200 = f'<span style="color:{COLOR_NEGATIVE};font-size:11px;"> ▼ Below 200-MA</span>'

        rows_html += f"""
        <tr>
          <td style="{_td_style(alt)}">
            {commodity_emojis.get(commodity,'')} <strong>{data.get('name', commodity.title())}</strong>
          </td>
          <td style="{_td_style(alt)};color:{rsi_color};font-weight:600;">
            {f"{rsi14:.1f}" if rsi14 is not None else "N/A"}{rsi_label}
          </td>
          <td style="{_td_style(alt)};">
            {_fmt_price(ma14, 4)}
          </td>
          <td style="{_td_style(alt)};">
            {_fmt_price(ma200, 4)} {vs_ma200}
          </td>
        </tr>"""

    return f"""
    <table style="{_table_style()}">
      <thead>
        <tr>
          <th style="{_th_style()}">Commodity</th>
          <th style="{_th_style()}">RSI (14)</th>
          <th style="{_th_style()}">14-Day MA</th>
          <th style="{_th_style()}">200-Day MA</th>
        </tr>
      </thead>
      <tbody>
        {rows_html}
      </tbody>
    </table>"""


def _build_elevator_bids_section(elevator_bids: dict[str, list[dict]]) -> str:
    """Build the per-elevator cash bid tables."""
    parts = []
    elevator_order = [
        "Scoular CBLOC",
        "Cargill East St. Louis",
        "Bunge Fairmount City",
        "Bartlett Jacksonville",
        "ADM Decatur Soy",
        "ADM Decatur Corn",
        "ADM Sauget",
        "CHS Illinois",
        "GPRe Madison",
        "CGB",
    ]
    # Show in defined order, then any extra elevators alphabetically
    all_names = list(elevator_bids.keys())
    ordered = [n for n in elevator_order if n in all_names]
    extras = sorted([n for n in all_names if n not in elevator_order])
    names_to_show = ordered + extras

    for elevator_name in names_to_show:
        bids = elevator_bids.get(elevator_name, [])
        parts.append(_build_single_elevator_table(elevator_name, bids))

    return "\n".join(parts) if parts else "<p style='color:#9ca3af;font-size:13px;'>No elevator bid data available.</p>"


def _build_single_elevator_table(elevator_name: str, bids: list[dict]) -> str:
    """Build the bid table for one elevator."""
    if not bids:
        return f"""
    <div style="margin-bottom:16px;padding:10px 14px;background:#fff7ed;
                border-left:3px solid {COLOR_WARNING};border-radius:0 4px 4px 0;">
      <span style="color:{COLOR_WARNING};font-weight:600;font-size:13px;">
        ⚠ {elevator_name}
      </span>
      <span style="color:#92400e;font-size:12px;margin-left:8px;">
        Failed to load bids — check site manually
      </span>
    </div>"""

    # Group bids by commodity
    commodities: dict[str, list[dict]] = {}
    for bid in bids:
        commodity = bid.get("commodity", "Unknown")
        if commodity not in commodities:
            commodities[commodity] = []
        commodities[commodity].append(bid)

    rows_html = ""
    row_idx = 0
    for commodity, cbids in sorted(commodities.items()):
        for bid in cbids:
            alt = row_idx % 2 == 1
            delivery = bid.get("delivery_period", "")
            cash = _fmt_cash(bid.get("cash_price"))
            basis = _fmt_basis(bid.get("basis"))
            futures_ref = bid.get("futures_reference", "")
            change = bid.get("change")
            change_color = _change_color(change)
            change_str = _fmt_change(change) if change is not None else "—"

            # Basis color
            basis_val = bid.get("basis")
            basis_color = COLOR_NEUTRAL
            if basis_val is not None:
                # Adjust for dollar vs cent scale
                effective_basis = basis_val * 100 if abs(basis_val) < 2.0 and basis_val != 0 else basis_val
                if effective_basis > 0:
                    basis_color = COLOR_POSITIVE
                elif effective_basis < 0:
                    basis_color = COLOR_NEGATIVE

            rows_html += f"""
          <tr>
            <td style="{_td_style(alt)}">{commodity}</td>
            <td style="{_td_style(alt)}">{delivery}</td>
            <td style="{_td_style(alt)};font-weight:600;">{cash}</td>
            <td style="{_td_style(alt)};color:{basis_color};font-weight:600;">{basis}</td>
            <td style="{_td_style(alt)};font-family:monospace;font-size:11px;">{futures_ref}</td>
            <td style="{_td_style(alt)};color:{change_color};">{change_str}</td>
          </tr>"""
            row_idx += 1

    return f"""
    <div style="margin-bottom:20px;">
      <div style="background:{COLOR_LIGHT_GREEN};padding:8px 12px;border-radius:4px 4px 0 0;
                  border-left:4px solid {COLOR_PRIMARY};">
        <strong style="color:{COLOR_PRIMARY};font-size:13px;font-family:Arial,sans-serif;">
          {elevator_name}
        </strong>
      </div>
      <table style="{_table_style()}margin-bottom:0;">
        <thead>
          <tr>
            <th style="{_th_style()}">Commodity</th>
            <th style="{_th_style()}">Delivery</th>
            <th style="{_th_style()}">Cash ($/bu)</th>
            <th style="{_th_style()}">Basis (¢)</th>
            <th style="{_th_style()}">Futures Ref</th>
            <th style="{_th_style()}">Change</th>
          </tr>
        </thead>
        <tbody>
          {rows_html}
        </tbody>
      </table>
    </div>"""


def _build_basis_trends_section(
    basis_trends: dict[str, dict],
    elevator_bids: dict[str, list[dict]],
) -> str:
    """Build the basis trends summary table."""
    if not basis_trends:
        return "<p style='color:#9ca3af;font-size:13px;'>Insufficient history for trend analysis.</p>"

    rows_html = ""
    row_idx = 0

    for key, trend in sorted(basis_trends.items()):
        parts = key.split("|", 1)
        elevator = parts[0] if len(parts) > 0 else key
        commodity = parts[1] if len(parts) > 1 else ""

        current = trend.get("current_basis")
        b1w = trend.get("basis_1week_ago")
        b2w = trend.get("basis_2weeks_ago")
        b1m = trend.get("basis_1month_ago")

        alt = row_idx % 2 == 1
        rows_html += f"""
        <tr>
          <td style="{_td_style(alt)};font-size:12px;">{elevator}</td>
          <td style="{_td_style(alt)};font-size:12px;">{commodity}</td>
          <td style="{_td_style(alt)};font-weight:600;">{_fmt_basis(current)}</td>
          <td style="{_td_style(alt)};color:#6b7280;">{_fmt_basis(b1w)}</td>
          <td style="{_td_style(alt)};color:#6b7280;">{_fmt_basis(b2w)}</td>
          <td style="{_td_style(alt)};color:#6b7280;">{_fmt_basis(b1m)}</td>
          <td style="{_td_style(alt)}">{_trend_badge(trend.get("trend_1week","N/A"))}</td>
          <td style="{_td_style(alt)}">{_trend_badge(trend.get("trend_2week","N/A"))}</td>
          <td style="{_td_style(alt)}">{_trend_badge(trend.get("trend_1month","N/A"))}</td>
        </tr>"""
        row_idx += 1

    if not rows_html:
        return "<p style='color:#9ca3af;font-size:13px;'>No basis trend data available.</p>"

    return f"""
    <table style="{_table_style()}font-size:12px;">
      <thead>
        <tr>
          <th style="{_th_style()}">Elevator</th>
          <th style="{_th_style()}">Commodity</th>
          <th style="{_th_style()}">Current</th>
          <th style="{_th_style()}">1 Wk Ago</th>
          <th style="{_th_style()}">2 Wk Ago</th>
          <th style="{_th_style()}">1 Mo Ago</th>
          <th style="{_th_style()}">1-Wk Trend</th>
          <th style="{_th_style()}">2-Wk Trend</th>
          <th style="{_th_style()}">1-Mo Trend</th>
        </tr>
      </thead>
      <tbody>
        {rows_html}
      </tbody>
    </table>"""


def _build_elevator_spreads_section(elevator_spreads: dict) -> str:
    """
    Build a table showing each elevator's front-month vs deferred cash bid spreads,
    with day-over-day spread change.
    """
    if not elevator_spreads:
        return (
            "<p style='color:#9ca3af;font-size:13px;'>"
            "Only one delivery month available per location — no spread data.</p>"
        )

    rows_html = ""
    row_idx = 0

    for key in sorted(elevator_spreads.keys()):
        parts = key.split("|", 1)
        elevator = parts[0] if parts else key
        commodity = parts[1] if len(parts) > 1 else ""
        spread_rows = elevator_spreads[key]

        for sr in spread_rows:
            alt = row_idx % 2 == 1
            spread_today = sr.get("spread_today")
            spread_yesterday = sr.get("spread_yesterday")
            spread_change = sr.get("spread_change")

            spread_color = _change_color(spread_today)
            spread_str = f"${spread_today:+.2f}" if spread_today is not None else "N/A"

            if spread_change is None:
                sc_str = "<span style='color:#9ca3af;font-size:11px;'>no prior data</span>"
            else:
                sc_color = _change_color(spread_change)
                arrow = "▲" if spread_change > 0.005 else ("▼" if spread_change < -0.005 else "→")
                sc_str = f'<span style="color:{sc_color};font-weight:600;">{arrow} ${spread_change:+.2f}</span>'

            yesterday_str = f"${spread_yesterday:+.2f}" if spread_yesterday is not None else "—"

            rows_html += f"""
        <tr>
          <td style="{_td_style(alt)};font-size:12px;">{elevator}</td>
          <td style="{_td_style(alt)};font-size:12px;">{commodity}</td>
          <td style="{_td_style(alt)};font-size:12px;">{sr.get('front_delivery','')}</td>
          <td style="{_td_style(alt)};font-size:12px;">{sr.get('deferred_delivery','')}</td>
          <td style="{_td_style(alt)};color:{spread_color};font-weight:600;">{spread_str}</td>
          <td style="{_td_style(alt)};color:#6b7280;">{yesterday_str}</td>
          <td style="{_td_style(alt)};">{sc_str}</td>
        </tr>"""
            row_idx += 1

    if not rows_html:
        return "<p style='color:#9ca3af;font-size:13px;'>No elevator deferred spread data available.</p>"

    return f"""
    <table style="{_table_style()}font-size:12px;">
      <thead>
        <tr>
          <th style="{_th_style()}">Elevator</th>
          <th style="{_th_style()}">Commodity</th>
          <th style="{_th_style()}">Front Month</th>
          <th style="{_th_style()}">Deferred Month</th>
          <th style="{_th_style()}">Spread Today</th>
          <th style="{_th_style()}">Spread Yesterday</th>
          <th style="{_th_style()}">Change</th>
        </tr>
      </thead>
      <tbody>
        {rows_html}
      </tbody>
    </table>"""


def _build_warnings(failed_elevators: list[str]) -> str:
    """Build a warning banner for failed elevator scrapers."""
    if not failed_elevators:
        return ""

    items_html = "".join(
        f'<li style="margin:2px 0;">{name}</li>' for name in failed_elevators
    )

    return f"""
    <div style="background:#fff7ed;border:1px solid #fed7aa;border-radius:6px;
                padding:12px 16px;margin-bottom:16px;">
      <div style="color:{COLOR_WARNING};font-weight:700;font-size:13px;margin-bottom:4px;">
        ⚠ Data Unavailable for {len(failed_elevators)} Elevator(s)
      </div>
      <ul style="margin:4px 0 0 16px;color:#92400e;font-size:12px;padding:0;">
        {items_html}
      </ul>
      <div style="color:#92400e;font-size:11px;margin-top:6px;">
        These sites may be temporarily down or require manual verification.
      </div>
    </div>"""
