import asyncio
import sys, os
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
except ImportError:
    AsyncIOScheduler = None
from services.live_aqi import fetch_live_aqi, CITY_COORDS
from database import SessionLocal
from models import City, AirQualityData, AQIHistory
from ml.predict import predict_aqi_risk
from config import get_settings

# Centralized settings
KEY = get_settings().OPENWEATHER_API_KEY

async def refresh_all_cities():
    """
    Auto-refresh live AQI data for all cities using centralized fetch_live_aqi.
    Ensures normalized CPCB units (pm25, pm10, no2, so2, o3 in ug/m3; co in mg/m3)
    are calculated and persisted consistently without duplicate conversion logic.
    Preserves source observation timestamps separately from application ingestion time.
    Prevents repeated scheduler runs from artificially refreshing old observations.
    """
    print("Auto-refreshing live AQI data...")
    db = SessionLocal()
    try:
        ingestion_time = datetime.utcnow()
        for name in CITY_COORDS.keys():
            try:
                live_data = await fetch_live_aqi(name)
                if not live_data:
                    continue
                city = db.query(City).filter(City.city_name == name).first()
                if not city:
                    continue
                rec = db.query(AirQualityData).filter(AirQualityData.city_id == city.city_id).first()
                criteria_pollutants = ["pm25", "pm10", "no2", "co", "so2", "o3"]
                if rec and all(live_data.get(k) is not None for k in criteria_pollutants):
                    # Distinguish source observation timestamp from application ingestion time
                    source_obs_time = None
                    if live_data.get("dt") is not None:
                        try:
                            source_obs_time = datetime.fromtimestamp(live_data["dt"], tz=timezone.utc).replace(tzinfo=None)
                        except Exception:
                            source_obs_time = None
                    elif live_data.get("observation_time"):
                        try:
                            dt_parsed = datetime.fromisoformat(live_data["observation_time"].replace("Z", "+00:00"))
                            source_obs_time = dt_parsed.astimezone(timezone.utc).replace(tzinfo=None) if dt_parsed.tzinfo else dt_parsed
                        except ValueError:
                            source_obs_time = None

                    # If source observation timestamp is not provided, fall back to ingestion_time
                    target_recorded_at = source_obs_time if source_obs_time is not None else ingestion_time

                    # Check if observation was already recorded with this source timestamp.
                    # A repeated scheduler run must NOT make an old source observation appear newly observed.
                    is_new_observation = (rec.recorded_at is None or rec.recorded_at != target_recorded_at)

                    rec.pm25 = live_data["pm25"]
                    rec.pm10 = live_data["pm10"]
                    rec.no2  = live_data["no2"]
                    rec.co   = live_data["co"]   # Normalized mg/m3 from centralized fetch_live_aqi
                    rec.so2  = live_data["so2"]
                    rec.o3   = live_data["o3"]
                    rec.recorded_at = target_recorded_at
                    rec.observation_time = source_obs_time
                    rec.ingested_at = ingestion_time
                    rec.data_source = live_data.get("data_source", "openweather")
                    rec.calculation_type = live_data.get("calculation_type", "instantaneous_estimate")
                    rec.calculation_formula = live_data.get("calculation_formula", "cpcb_formula")
                    rec.is_cpcb_compliant = live_data.get("is_cpcb_compliant", False)
                    rec.verification_status = live_data.get("verification_status", "vendor_live_telemetry")
                    rec.averaging_period_status = live_data.get("averaging_period_status", "single_hour_instantaneous")
                    rec.averaging_periods = live_data.get("averaging_periods")
                    rec.provenance = live_data.get("provenance")

                    p = predict_aqi_risk(rec.pm25, rec.pm10, rec.no2, rec.co, rec.so2, rec.o3)
                    rec.aqi_score = p["predicted_aqi"]
                    
                    # Only add to historical records if this is a newly observed reading
                    if is_new_observation:
                        hist = AQIHistory(
                            city_id=city.city_id,
                            city_name=city.city_name,
                            pm25=rec.pm25,
                            pm10=rec.pm10,
                            aqi_score=rec.aqi_score,
                            risk_level=p["risk_level"],
                            recorded_at=target_recorded_at,
                            observation_time=source_obs_time,
                            ingested_at=ingestion_time,
                            data_source=rec.data_source,
                            calculation_type=rec.calculation_type,
                            calculation_formula=rec.calculation_formula,
                            is_cpcb_compliant=rec.is_cpcb_compliant,
                            verification_status=rec.verification_status,
                            averaging_period_status=rec.averaging_period_status,
                            averaging_periods=rec.averaging_periods,
                            provenance=rec.provenance
                        )
                        db.add(hist)
            except Exception as e:
                print(f"Error refreshing {name}: {e}")
        db.commit()
    except Exception as exc:
        db.rollback()
        print(f"Error refreshing cities: {exc}")
    finally:
        db.close()
    print("Refresh complete")

def start_scheduler():
    if AsyncIOScheduler is None:
        raise RuntimeError("apscheduler is not installed")
    scheduler = AsyncIOScheduler()
    scheduler.add_job(refresh_all_cities, "interval", minutes=30)
    scheduler.start()
    return scheduler
