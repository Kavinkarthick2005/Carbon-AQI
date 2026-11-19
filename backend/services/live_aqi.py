import os
import time
import math
from datetime import datetime, timezone
import httpx
from config import get_settings

# Centralized settings

API_KEY = get_settings().OPENWEATHER_API_KEY

_cache = {}
_cache_ttl = 1800  # 30 minutes

def normalize_openweather_components(comp: dict, dt: int | None = None) -> dict:
    """
    Normalize raw OpenWeather air pollution components (all reported in ug/m3)
    to standard CPCB concentration units:
      - pm25: ug/m3
      - pm10: ug/m3
      - no2:  ug/m3
      - so2:  ug/m3
      - o3:   ug/m3
      - co:   mg/m3 (converted from ug/m3 via / 1000.0 exactly once)
      
    Distinguishes explicit measured zeros from missing data.
    OpenWeather 'main.aqi' is an index (1-5), NOT a concentration, and is excluded.
    Flags reading explicitly as an instantaneous single-hour estimate.
    """
    def _extract(key: str, divisor: float = 1.0, decimals: int = 2) -> float | None:
        if comp is None or key not in comp or comp[key] is None:
            return None
        try:
            val = float(comp[key])
            if not math.isfinite(val) or val < 0:
                return None
            return round(val / divisor, decimals)
        except (ValueError, TypeError):
            return None

    obs_time = None
    if dt is not None:
        try:
            obs_time = datetime.fromtimestamp(dt, tz=timezone.utc).isoformat()
        except Exception:
            obs_time = None

    ingested_time = datetime.now(timezone.utc).isoformat()

    averaging_windows = {
        "pm25": "1h",
        "pm10": "1h",
        "no2": "1h",
        "co": "1h",
        "so2": "1h",
        "o3": "1h"
    }
    provenance_metadata = {
        "provider": "openweather",
        "endpoint": "air_pollution",
        "source_dt_epoch": dt,
        "retrieval_time": ingested_time,
        "pollutant_units": {
            "pm25": "ug/m3",
            "pm10": "ug/m3",
            "no2": "ug/m3",
            "so2": "ug/m3",
            "o3": "ug/m3",
            "co": "mg/m3"
        },
        "calculation_type": "instantaneous_estimate",
        "is_cpcb_compliant": False
    }

    return {
        "pm25": _extract("pm2_5"),
        "pm10": _extract("pm10"),
        "no2":  _extract("no2"),
        "co":   _extract("co", divisor=1000.0, decimals=3),
        "so2":  _extract("so2"),
        "o3":   _extract("o3"),
        "source": "live",
        "data_source": "openweather",
        "dt": dt,
        "observation_time": obs_time,
        "observed_at": obs_time,
        "timestamp": obs_time,
        "ingested_at": ingested_time,
        "calculation_type": "instantaneous_estimate",
        "calculation_formula": "cpcb_formula",
        "is_cpcb_compliant": False,
        "verification_status": "vendor_live_telemetry",
        "averaging_period_status": "single_hour_instantaneous",
        "averaging_periods": averaging_windows,
        "provenance": provenance_metadata,
        "cpcb_status": "insufficient_data"
    }

def is_observation_stale(
    observation_time: datetime | str | int | float | None,
    max_age_hours: float = 24.0,
    ref_time: datetime | None = None
) -> bool:
    """
    Determine whether an observation is stale based on its source observation time.
    Consistent UTC policy: compares naive UTC datetimes safely without mixing naive and aware.
    Returns True if observation_time is None (freshness unverified) or exceeds max_age_hours.
    """
    if observation_time is None:
        return True

    obs_dt = None
    if isinstance(observation_time, (int, float)):
        try:
            obs_dt = datetime.fromtimestamp(observation_time, tz=timezone.utc).replace(tzinfo=None)
        except Exception:
            return True
    elif isinstance(observation_time, datetime):
        if observation_time.tzinfo is not None:
            obs_dt = observation_time.astimezone(timezone.utc).replace(tzinfo=None)
        else:
            obs_dt = observation_time
    elif isinstance(observation_time, str):
        try:
            dt = datetime.fromisoformat(observation_time.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                obs_dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            else:
                obs_dt = dt
        except ValueError:
            return True

    if obs_dt is None:
        return True

    if ref_time is None:
        ref_dt = datetime.utcnow()
    elif ref_time.tzinfo is not None:
        ref_dt = ref_time.astimezone(timezone.utc).replace(tzinfo=None)
    else:
        ref_dt = ref_time

    age_seconds = (ref_dt - obs_dt).total_seconds()
    return age_seconds > (max_age_hours * 3600)

CITY_COORDS = {
    "Delhi":     (28.6139, 77.2090),
    "Mumbai":    (19.0760, 72.8777),
    "Bangalore": (12.9716, 77.5946),
    "Chennai":   (13.0827, 80.2707),
    "Kolkata":   (22.5726, 88.3639),
    "Hyderabad": (17.3850, 78.4867),
    "Pune":      (18.5204, 73.8567),
    "Ahmedabad": (23.0225, 72.5714),
    "Lucknow":   (26.8467, 80.9462),
    "Jaipur":    (26.9124, 75.7873),
}

async def fetch_live_aqi(city_name: str) -> dict | None:
    coords = CITY_COORDS.get(city_name)
    if not coords or not API_KEY:
        return None
    lat, lon = coords

    cached = _cache.get(city_name)
    if cached and (time.time() - cached["ts"]) < _cache_ttl:
        return cached["data"]

    url = f"http://api.openweathermap.org/data/2.5/air_pollution?lat={lat}&lon={lon}&appid={API_KEY}"
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(url, timeout=10)
            if r.status_code != 200:
                return cached["data"] if cached else None
            data = r.json()
            comp = data["list"][0]["components"]
            dt = data["list"][0].get("dt")
            result = normalize_openweather_components(comp, dt=dt)
            _cache[city_name] = {"data": result, "ts": time.time()}
            return result
    except Exception as e:
        print(f"Live AQI fetch error for {city_name}: {e}")
        return cached["data"] if cached else None

async def fetch_all_cities_live() -> list:
    results = []
    for city_name, (lat, lon) in CITY_COORDS.items():
        data = await fetch_live_aqi(city_name)
        if data:
            data["city"] = city_name
            results.append(data)
    return results
