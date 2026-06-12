"""
Gmail SMTP delivery for apartment alert batches.

Uses SMTP_SSL (port 465) with a Gmail App Password — NOT your regular password.
See SETUP.md for how to create one.
"""

import logging
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from .models import Listing

logger = logging.getLogger(__name__)

TIER_LABELS = {1: "⭐ TIER 1", 2: "TIER 2"}


def send_alert_email(
    matches: list[tuple[Listing, dict]],
    to_addr: str,
    from_addr: str,
    password: str,
) -> None:
    """Send an HTML batch email with all scored matches (sorted by score desc)."""
    if not matches:
        return

    subject = (
        f"SF Apt Alert — {len(matches)} new match{'es' if len(matches) != 1 else ''} "
        f"({datetime.now().strftime('%a %b %-d, %-I:%M %p')})"
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr

    html_body = _build_html(matches)
    plain_body = _build_plain(matches)

    msg.attach(MIMEText(plain_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(from_addr, password)
        smtp.sendmail(from_addr, to_addr, msg.as_string())

    logger.info(f"Sent alert email to {to_addr} with {len(matches)} matches")


# ------------------------------------------------------------------ #
# HTML template                                                        #
# ------------------------------------------------------------------ #

_CONTAINER_STYLE = (
    "font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; "
    "max-width: 680px; margin: 0 auto; background: #f8f8f8; padding: 24px 0;"
)

_CARD_STYLE = (
    "background: #ffffff; border-radius: 8px; margin: 0 0 20px 0; "
    "overflow: hidden; border: 1px solid #e0e0e0;"
)

_HEADER_STYLE = (
    "background: #1a1a2e; color: #ffffff; padding: 20px 24px 16px; "
    "font-size: 22px; font-weight: 700; letter-spacing: -0.5px;"
)

_SUBHEADER_STYLE = "color: #aaaacc; font-size: 13px; margin-top: 4px; font-weight: 400;"

_BODY_STYLE = "padding: 16px 24px 20px;"

_PHOTO_STYLE = (
    "width: 100%; max-height: 240px; object-fit: cover; display: block; "
    "border-bottom: 1px solid #e0e0e0;"
)

_SCORE_BAR_STYLE = (
    "display: inline-block; background: #e8f5e9; color: #2e7d32; "
    "border-radius: 4px; padding: 2px 8px; font-weight: 700; font-size: 13px; "
    "margin-right: 6px;"
)

_LIGHT_BAR_STYLE = (
    "display: inline-block; background: #fff9c4; color: #f57f17; "
    "border-radius: 4px; padding: 2px 8px; font-weight: 700; font-size: 13px; "
    "margin-right: 6px;"
)

_TIER1_BADGE = (
    "display: inline-block; background: #7c3aed; color: #fff; "
    "border-radius: 4px; padding: 2px 8px; font-size: 12px; font-weight: 600; "
    "margin-right: 6px;"
)

_TIER2_BADGE = (
    "display: inline-block; background: #2563eb; color: #fff; "
    "border-radius: 4px; padding: 2px 8px; font-size: 12px; font-weight: 600; "
    "margin-right: 6px;"
)

_UNCERTAIN_BADGE = (
    "display: inline-block; background: #d97706; color: #fff; "
    "border-radius: 4px; padding: 2px 8px; font-size: 12px; font-weight: 600; "
    "margin-right: 6px;"
)

_CTA_STYLE = (
    "display: inline-block; background: #1a1a2e; color: #ffffff !important; "
    "text-decoration: none; padding: 10px 20px; border-radius: 6px; "
    "font-weight: 600; font-size: 14px; margin-top: 12px;"
)

_REASONING_STYLE = (
    "color: #444; font-size: 14px; line-height: 1.5; "
    "border-left: 3px solid #1a1a2e; padding-left: 12px; margin: 10px 0 0;"
)

_PRIVACY_STYLE = "color: #666; font-size: 13px; margin: 6px 0 0;"


def _tier_badge(listing: Listing) -> str:
    if listing.neighborhood_tier == 1:
        return f'<span style="{_TIER1_BADGE}">⭐ TIER 1</span>'
    elif listing.neighborhood_tier == 2:
        return f'<span style="{_TIER2_BADGE}">TIER 2</span>'
    elif listing.neighborhood_uncertain:
        return f'<span style="{_UNCERTAIN_BADGE}">⚠ UNCERTAIN AREA</span>'
    return ""


def _card_html(rank: int, listing: Listing, score_data: dict) -> str:
    photo_html = ""
    if listing.photo_urls:
        photo_html = (
            f'<img src="{listing.photo_urls[0]}" alt="Listing photo" '
            f'style="{_PHOTO_STYLE}">'
        )

    beds_str = f"{listing.bedrooms}BR" if listing.bedrooms else "?"
    sqft_str = f" · {listing.sqft:,} ft²" if listing.sqft else ""

    price_color = "#c0392b" if listing.price >= 4500 else "#1a1a2e"

    return f"""
<div style="{_CARD_STYLE}">
  {photo_html}
  <div style="{_BODY_STYLE}">
    <div style="color: #888; font-size: 12px; margin-bottom: 6px;">#{rank}</div>
    <div style="font-size: 20px; font-weight: 700; color: #1a1a2e; margin-bottom: 6px;">
      <a href="{listing.url}" style="color: #1a1a2e; text-decoration: none;">
        {listing.title}
      </a>
    </div>
    <div style="margin-bottom: 10px;">
      <span style="font-size: 18px; font-weight: 700; color: {price_color};">
        ${listing.price:,}/mo
      </span>
      <span style="color: #666; font-size: 14px; margin-left: 8px;">
        {beds_str}{sqft_str}
      </span>
    </div>
    <div style="margin-bottom: 10px;">
      {_tier_badge(listing)}
      <span style="color: #444; font-size: 14px;">{listing.neighborhood_name or listing.location}</span>
    </div>
    <div style="margin-bottom: 12px;">
      <span style="{_SCORE_BAR_STYLE}">Score {score_data['score']}/10</span>
      <span style="{_LIGHT_BAR_STYLE}">☀ Light {score_data['lighting_score']}/10</span>
    </div>
    <p style="{_REASONING_STYLE}">{score_data['reasoning']}</p>
    <p style="{_PRIVACY_STYLE}"><strong>Privacy:</strong> {score_data['privacy_notes']}</p>
    <a href="{listing.url}" style="{_CTA_STYLE}">View Listing →</a>
  </div>
</div>"""


def _build_html(matches: list[tuple[Listing, dict]]) -> str:
    now = datetime.now().strftime("%A, %B %-d · %-I:%M %p")
    cards = "\n".join(
        _card_html(i + 1, listing, score_data)
        for i, (listing, score_data) in enumerate(matches)
    )
    footer = (
        '<div style="text-align: center; color: #aaa; font-size: 12px; '
        'padding: 16px;">apartment-alert · sfbay.craigslist.org/sfc/apa</div>'
    )
    header = f"""
<div style="{_HEADER_STYLE}">
  SF Apartment Alert
  <div style="{_SUBHEADER_STYLE}">{now} · {len(matches)} new match{'es' if len(matches) != 1 else ''}</div>
</div>"""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f0f0f0;">
<div style="{_CONTAINER_STYLE}">
  {header}
  <div style="padding: 20px 16px 8px;">
    {cards}
  </div>
  {footer}
</div>
</body>
</html>"""


def _build_plain(matches: list[tuple[Listing, dict]]) -> str:
    lines = [
        "SF APARTMENT ALERT",
        f"{datetime.now().strftime('%A, %B %-d at %-I:%M %p')} · {len(matches)} new matches",
        "=" * 60,
    ]
    for i, (listing, score_data) in enumerate(matches, 1):
        tier = TIER_LABELS.get(listing.neighborhood_tier, "Uncertain area")
        lines += [
            f"\n#{i} — Score {score_data['score']}/10 · Light {score_data['lighting_score']}/10",
            f"{listing.title}",
            f"${listing.price:,}/mo · {listing.bedrooms or '?'}BR · {listing.neighborhood_name} [{tier}]",
            f"{score_data['reasoning']}",
            f"Privacy: {score_data['privacy_notes']}",
            listing.url,
            "-" * 60,
        ]
    return "\n".join(lines)
