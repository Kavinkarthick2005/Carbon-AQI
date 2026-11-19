from fastapi import APIRouter, Query
from typing import Optional

from services.alert_engine import get_active_alerts_for_city, get_active_alerts_for_all_cities

router = APIRouter()


@router.get("/active")
async def get_active_alerts(city_id: Optional[str] = Query(None)):
    """
    GET /api/alerts/active

    Public Access Policy:
    --------------------
    This endpoint is intentionally public and unauthenticated.
    It returns real-time city-level environmental hazard alerts (CRITICAL, WARNING,
    ADVISORY) for general public safety and awareness.
    The response payload contains solely environmental data and recommended public
    health strategies; it contains no personal identifiable information (PII),
    user IDs, account credentials, or private bookmarks.
    """
    if city_id:
        return get_active_alerts_for_city(city_id)
    return get_active_alerts_for_all_cities()
