from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Listing:
    cl_id: str
    title: str
    price: int
    location: str
    url: str
    lat: Optional[float] = None
    lng: Optional[float] = None
    bedrooms: Optional[int] = None
    sqft: Optional[int] = None
    description: str = ""
    photo_urls: list = field(default_factory=list)
    posted_at: Optional[str] = None
    neighborhood_tier: Optional[int] = None   # 1, 2, or None
    neighborhood_name: Optional[str] = None
    neighborhood_uncertain: bool = False


@dataclass
class ScoreResult:
    match: bool
    score: int           # 1-10
    lighting_score: int  # 1-10
    privacy_notes: str
    reasoning: str
