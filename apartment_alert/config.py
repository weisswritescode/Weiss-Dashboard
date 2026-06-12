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
REQUEST_DELAY: float = 2.5       # seconds between Craigslist requests
MAX_PHOTOS_PER_LISTING: int = 4  # max photos sent to Claude per listing
MAX_LISTINGS_TO_SCORE: int = 25  # cap per run to limit API cost
MIN_SCORE_TO_EMAIL: int = 5      # only include listings scoring >= this
