import os
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")
GMAIL_APP_PASSWORD: str = os.environ.get("GMAIL_APP_PASSWORD", "")
GMAIL_FROM: str = os.environ.get("GMAIL_FROM", "adam.jacob.weiss@gmail.com")
GMAIL_TO: str = os.environ.get("GMAIL_TO", "adam.jacob.weiss@gmail.com")
DB_PATH: str = os.environ.get("DB_PATH", "apartment_alert.db")

MAX_PRICE: int = 5000
MIN_BEDROOMS: int = 1
REQUEST_DELAY: float = 2.5       # seconds between requests to any single source
MAX_PHOTOS_PER_LISTING: int = 4  # max photos sent to Claude per listing
MAX_LISTINGS_TO_SCORE: int = 25  # cap on Claude scoring calls per run (cost control)
MIN_SCORE_TO_EMAIL: int = 5      # minimum overall_score to include in email

# Sources to enable (set ENABLE_ZILLOW=false in .env to skip Zillow)
ENABLE_CRAIGSLIST: bool = os.environ.get("ENABLE_CRAIGSLIST", "true").lower() != "false"
ENABLE_ZILLOW: bool = os.environ.get("ENABLE_ZILLOW", "true").lower() != "false"
