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

# Scorecard dimensions shown in the email card (label, score_key, bar color)
_DIMENSIONS = [
    ("Value",        "value_score",        "#10b981"),  # emerald
    ("Location",     "location_score",     "#6366f1"),  # indigo
    ("Room Quality", "room_quality_score", "#3b82f6"),  # blue
    ("Cleanliness",  "cleanliness_score",  "#14b8a6"),  # teal
    ("Style",        "style_score",        "#f59e0b"),  # amber
    ("Lighting ☀",   "lighting_score",     "#f97316"),  # orange
]


def send_alert_email(
    matches: list[tuple[Listing, dict]],
    to_addr: str,
    from_addr: str,
    password: str,
) -> None:
    """Send an HTML batch email with all scored matches sorted by overall score."""
    if not matches:
        return

    count = len(matches)
    subject = (
        f"SF Apt Alert — {count} new match{'es' if count != 1 else ''} "
        f"({datetime.now().strftime('%a %b %-d, %-I:%M %p')})"
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr

    msg.attach(MIMEText(_build_plain(matches), "plain"))
    msg.attach(MIMEText(_build_html(matches), "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(from_addr, password)
        smtp.sendmail(from_addr, to_addr, msg.as_string())

    logger.info(f"Sent alert email to {to_addr} with {count} matches")


# ------------------------------------------------------------------ #
# HTML helpers                                                         #
# ------------------------------------------------------------------ #

def _score_color(score: int) -> str:
    if score >= 8:
        return "#16a34a"   # green
    elif score >= 6:
        return "#ca8a04"   # amber
    else:
        return "#dc2626"   # red


def _score_bar_row(label: str, score: int, bar_color: str) -> str:
    pct = score * 10
    score_color = _score_color(score)
    return f"""
    <tr>
      <td style="padding:3px 0; color:#555; font-size:12px; width:100px; white-space:nowrap;">
        {label}
      </td>
      <td style="padding:3px 8px; width:120px;">
        <div style="background:#e5e7eb; border-radius:3px; height:7px; overflow:hidden;">
          <div style="background:{bar_color}; width:{pct}%; height:7px; border-radius:3px;"></div>
        </div>
      </td>
      <td style="padding:3px 0; font-size:12px; font-weight:700; color:{score_color}; white-space:nowrap;">
        {score}/10
      </td>
    </tr>"""


def _tier_badge(listing: Listing) -> str:
    if listing.neighborhood_tier == 1:
        s = "display:inline-block;background:#7c3aed;color:#fff;border-radius:4px;padding:2px 8px;font-size:12px;font-weight:600;margin-right:6px;"
        return f'<span style="{s}">⭐ TIER 1</span>'
    elif listing.neighborhood_tier == 2:
        s = "display:inline-block;background:#2563eb;color:#fff;border-radius:4px;padding:2px 8px;font-size:12px;font-weight:600;margin-right:6px;"
        return f'<span style="{s}">TIER 2</span>'
    elif listing.neighborhood_uncertain:
        s = "display:inline-block;background:#d97706;color:#fff;border-radius:4px;padding:2px 8px;font-size:12px;font-weight:600;margin-right:6px;"
        return f'<span style="{s}">⚠ UNCERTAIN</span>'
    return ""


def _source_badge(listing: Listing) -> str:
    source = getattr(listing, "source", "craigslist")
    colors = {
        "craigslist": ("#6b7280", "CL"),
        "zillow":     ("#1d4ed8", "Zillow"),
    }
    bg, label = colors.get(source, ("#6b7280", source.upper()[:6]))
    s = (
        f"display:inline-block;background:{bg};color:#fff;"
        "border-radius:4px;padding:2px 6px;font-size:11px;font-weight:600;margin-right:6px;"
    )
    return f'<span style="{s}">{label}</span>'


def _card_html(rank: int, listing: Listing, sd: dict) -> str:
    photo_html = ""
    if listing.photo_urls:
        photo_html = (
            f'<a href="{listing.url}" style="display:block;">'
            f'<img src="{listing.photo_urls[0]}" alt="" '
            f'style="width:100%;max-height:240px;object-fit:cover;display:block;'
            f'border-bottom:1px solid #e0e0e0;"></a>'
        )

    beds_str  = f"{listing.bedrooms}BR" if listing.bedrooms else "?"
    sqft_str  = f" · {listing.sqft:,} ft²" if listing.sqft else ""
    price_color = "#b91c1c" if listing.price >= 4500 else "#111827"
    overall = sd.get("overall_score", sd.get("score", 0))
    overall_color = _score_color(overall)

    # Build two-column scorecard table (3 rows × 2 cols)
    dim_pairs = [_DIMENSIONS[i:i+2] for i in range(0, len(_DIMENSIONS), 2)]
    scorecard_rows = ""
    for pair in dim_pairs:
        cells = ""
        for label, key, color in pair:
            score = sd.get(key, 0)
            pct = score * 10
            sc = _score_color(score)
            cells += f"""
        <td style="padding:4px 8px 4px 0; vertical-align:top; width:50%;">
          <div style="color:#555;font-size:11px;margin-bottom:2px;">{label}</div>
          <div style="display:flex;align-items:center;gap:6px;">
            <div style="flex:1;background:#e5e7eb;border-radius:3px;height:6px;overflow:hidden;min-width:60px;">
              <div style="background:{color};width:{pct}%;height:6px;border-radius:3px;"></div>
            </div>
            <span style="font-size:12px;font-weight:700;color:{sc};white-space:nowrap;">{score}/10</span>
          </div>
        </td>"""
        scorecard_rows += f"<tr>{cells}</tr>"

    cta_style = (
        "display:inline-block;background:#111827;color:#fff!important;"
        "text-decoration:none;padding:9px 18px;border-radius:6px;"
        "font-weight:600;font-size:13px;margin-top:14px;"
    )
    reasoning_style = (
        "color:#374151;font-size:13px;line-height:1.55;"
        "border-left:3px solid #111827;padding-left:10px;margin:10px 0 0;"
    )

    return f"""
<div style="background:#fff;border-radius:8px;margin:0 0 20px;overflow:hidden;border:1px solid #e0e0e0;">
  {photo_html}
  <div style="padding:16px 20px 18px;">
    <div style="color:#9ca3af;font-size:11px;margin-bottom:6px;">
      #{rank} &nbsp;{_source_badge(listing)}{_tier_badge(listing)}
    </div>
    <div style="font-size:18px;font-weight:700;color:#111827;margin-bottom:6px;line-height:1.3;">
      <a href="{listing.url}" style="color:#111827;text-decoration:none;">{listing.title}</a>
    </div>
    <div style="margin-bottom:10px;">
      <span style="font-size:17px;font-weight:800;color:{price_color};">${listing.price:,}/mo</span>
      <span style="color:#6b7280;font-size:13px;margin-left:8px;">{beds_str}{sqft_str}</span>
      <span style="color:#6b7280;font-size:13px;margin-left:8px;">·</span>
      <span style="color:#374151;font-size:13px;margin-left:8px;">{listing.neighborhood_name or listing.location}</span>
    </div>

    <!-- Overall score badge -->
    <div style="display:inline-block;background:#f0fdf4;border:1px solid #bbf7d0;
                border-radius:6px;padding:6px 14px;margin-bottom:12px;">
      <span style="font-size:12px;color:#166534;font-weight:600;">OVERALL</span>
      <span style="font-size:20px;font-weight:800;color:{overall_color};margin-left:6px;">{overall}</span>
      <span style="font-size:12px;color:#166534;">/10</span>
    </div>

    <!-- Dimension scorecard -->
    <table style="width:100%;border-collapse:collapse;margin-bottom:10px;">
      {scorecard_rows}
    </table>

    <p style="{reasoning_style}">{sd.get('reasoning', '')}</p>
    <p style="color:#6b7280;font-size:12px;margin:6px 0 0;">
      <strong style="color:#374151;">Privacy:</strong> {sd.get('privacy_notes', '')}
    </p>
    <a href="{listing.url}" style="{cta_style}">View Listing →</a>
  </div>
</div>"""


def _build_html(matches: list[tuple[Listing, dict]]) -> str:
    now = datetime.now().strftime("%A, %B %-d · %-I:%M %p")
    count = len(matches)
    cards = "\n".join(_card_html(i + 1, lst, sd) for i, (lst, sd) in enumerate(matches))

    header_style = (
        "background:#111827;color:#fff;padding:20px 24px 16px;"
        "font-size:22px;font-weight:700;letter-spacing:-0.5px;"
    )
    sub_style = "color:#9ca3af;font-size:13px;margin-top:4px;font-weight:400;"
    container_style = (
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        "max-width:680px;margin:0 auto;background:#f3f4f6;padding:24px 0;"
    )

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#e5e7eb;">
<div style="{container_style}">
  <div style="{header_style}">
    SF Apartment Alert
    <div style="{sub_style}">{now} · {count} new match{'es' if count != 1 else ''}</div>
  </div>
  <div style="padding:20px 16px 8px;">
    {cards}
  </div>
  <div style="text-align:center;color:#9ca3af;font-size:11px;padding:12px;">
    apartment-alert · craigslist + zillow · SF only
  </div>
</div>
</body>
</html>"""


def _build_plain(matches: list[tuple[Listing, dict]]) -> str:
    lines = [
        "SF APARTMENT ALERT",
        f"{datetime.now().strftime('%A, %B %-d at %-I:%M %p')} · {len(matches)} new matches",
        "=" * 60,
    ]
    for i, (lst, sd) in enumerate(matches, 1):
        tier = TIER_LABELS.get(lst.neighborhood_tier, "Uncertain")
        overall = sd.get("overall_score", sd.get("score", "?"))
        source = getattr(lst, "source", "craigslist").upper()
        lines += [
            f"\n#{i} — Overall {overall}/10  [{source}]",
            f"  Value {sd.get('value_score','?')}/10 · Location {sd.get('location_score','?')}/10 · "
            f"Room {sd.get('room_quality_score','?')}/10 · Clean {sd.get('cleanliness_score','?')}/10 · "
            f"Style {sd.get('style_score','?')}/10 · Light {sd.get('lighting_score','?')}/10",
            f"  {lst.title}",
            f"  ${lst.price:,}/mo · {lst.bedrooms or '?'}BR · {lst.neighborhood_name} [{tier}]",
            f"  {sd.get('reasoning', '')}",
            f"  Privacy: {sd.get('privacy_notes', '')}",
            f"  {lst.url}",
            "  " + "-" * 58,
        ]
    return "\n".join(lines)
