# The Daily Brief - Automated Grain Market Intelligence System
#
# Modules:
#   daily_brief   - Main orchestrator (entry point: python -m src.daily_brief)
#   futures       - CBOT futures prices + RSI/MA via yfinance + ta
#   elevator_bids - Playwright scrapers for 10 grain elevator websites
#   basis_tracker - CSV storage + 1-week/2-week/1-month basis trend calc
#   email_builder - HTML email template builder
#   mailer        - Gmail SMTP sender
