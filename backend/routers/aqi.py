from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func, desc
from typing import List, Dict, Any
from datetime import datetime, timedelta
import uuid

from database import get_db
from models import AirQualityData, MLPrediction, MitigationStrategy, City, AQIHistory, User
from schemas import AQIRecord, AQIResponse
from routers.auth import verify_firebase_token, require_admin_user, check_admin_role

# Import ML functions
from ml.predict import predict_aqi_risk, calculate_aqi, validate_cpcb_averaging_periods, _parse_timestamp
from ml.rl_agent import get_mitigation_strategy

from services.live_aqi import fetch_live_aqi, fetch_all_cities_live

router = APIRouter()

@router.post("/record", response_model=AQIResponse)
async def save_aqi_record(
    aqi_data: AQIRecord,
    admin_user: User = Depends(require_admin_user),
    db: Session = Depends(get_db)
):
    """
    Save a new AQI reading with ML prediction and RL strategy (Admin only).
    Enforces the CPCB standard unit contract:
      - pm25, pm10, no2, so2, o3: ug/m3
      - co: mg/m3
      - co2_ppm: ppm (optional, tracked separately from CO)
    Authoritative server-side calculation ignores any client-supplied aqi_score.
    """
    
    # Validate city_id format
    try:
        city_uuid = uuid.UUID(aqi_data.city_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="Invalid city_id format")
    
    # Find city by city_id
    city = db.query(City).filter(City.city_id == city_uuid).first()
    if not city:
        raise HTTPException(status_code=404, detail="City not found")
    
    # Authoritative server-side CPCB AQI calculation
    # All 6 criteria pollutants are validated and required. Client-provided aqi_score is ignored.
    calculated_aqi = calculate_aqi(
        aqi_data.pm25,
        aqi_data.pm10,
        aqi_data.no2,
        aqi_data.co,
        aqi_data.so2,
        aqi_data.o3,
    )
    authoritative_aqi = round(float(calculated_aqi), 2)
    
    # Distinguish observation timestamp from application ingestion timestamp
    ingestion_time = datetime.utcnow()
    parsed_obs_time = _parse_timestamp(aqi_data.observation_time) if aqi_data.observation_time else None
    recorded_timestamp = parsed_obs_time if parsed_obs_time is not None else ingestion_time

    # Validate user-declared averaging periods against CPCB standards
    matches_periods, period_status, avg_summary, reason = validate_cpcb_averaging_periods(
        aqi_data.averaging_periods,
        aqi_data.declared_averaging_period
    )
    calc_status = "valid" if matches_periods else ("invalid_averaging_period" if aqi_data.averaging_periods else "unverified")
    verification_status = "user_declared_admin"
    provenance_metadata = {
        "provider": "admin_manual_entry",
        "declared_source": aqi_data.source_provenance or "admin_manual_entry",
        "declared_averaging_period": aqi_data.declared_averaging_period,
        "admin_user_id": str(admin_user.user_id),
        "calculation_type": "manual_submission",
        "verification_status": "user_declared_admin",
        "is_cpcb_compliant": False
    }

    # Create new AQI record with admin provenance and authoritative calculated AQI
    new_record = AirQualityData(
        city_id=city_uuid,
        user_id=admin_user.user_id,
        pm25=aqi_data.pm25,
        pm10=aqi_data.pm10,
        co=aqi_data.co,
        no2=aqi_data.no2,
        so2=aqi_data.so2,
        o3=aqi_data.o3,
        co2_ppm=aqi_data.co2_ppm,
        aqi_score=authoritative_aqi,
        recorded_at=recorded_timestamp,
        observation_time=parsed_obs_time,
        ingested_at=ingestion_time,
        data_source="admin_manual_submission",
        calculation_type="manual_submission",
        calculation_formula="cpcb_formula",
        is_cpcb_compliant=False,
        verification_status=verification_status,
        averaging_period_status=period_status,
        averaging_periods=aqi_data.averaging_periods,
        provenance=provenance_metadata
    )
    
    db.add(new_record)
    db.commit()
    db.refresh(new_record)
    
    # Call ML prediction with correct positional parameters
    try:
        ml_result = predict_aqi_risk(
            aqi_data.pm25, 
            aqi_data.pm10, 
            aqi_data.no2, 
            aqi_data.co, 
            aqi_data.so2, 
            aqi_data.o3
        )
        
        # Save ML prediction
        ml_prediction = MLPrediction(
            record_id=new_record.record_id,
            predicted_aqi=ml_result['predicted_aqi'],
            risk_level=ml_result['risk_level'],
            model_version=ml_result.get('model_version', 'v1.0')
        )
        db.add(ml_prediction)
        
        # If risk level is High, call RL strategy
        if ml_result['risk_level'].lower() == 'high':
            try:
                rl_result = get_mitigation_strategy(
                    aqi_data.pm25, 
                    aqi_data.pm10, 
                    aqi_data.no2, 
                    aqi_data.co, 
                    aqi_data.so2, 
                    aqi_data.o3
                )
                
                # Save RL strategy
                strategy = MitigationStrategy(
                    record_id=new_record.record_id,
                    strategy_name=rl_result['strategy_name'],
                    description=rl_result['description'],
                    rl_reward=rl_result.get('rl_reward', 0.0)
                )
                db.add(strategy)
            except Exception as e:
                print(f"RL strategy error: {e}")
        
        db.commit()
        
        provenance = aqi_data.source_provenance or "admin_manual_entry"

        # Prepare response returning authoritative server-calculated AQI and explicit provenance
        response_data = {
            "record_id": new_record.record_id,
            "city_id": str(city_uuid),
            "pm25": new_record.pm25,
            "pm10": new_record.pm10,
            "no2": new_record.no2,
            "co": new_record.co,
            "so2": new_record.so2,
            "o3": new_record.o3,
            "aqi_score": authoritative_aqi,
            "recorded_at": new_record.recorded_at,
            "observation_time": parsed_obs_time,  # None if client did not supply observation_time
            "ingested_at": ingestion_time,
            "risk_level": ml_result['risk_level'],
            "calculation_type": "manual_submission",
            "calculation_formula": "cpcb_formula",
            "calculation_status": calc_status,
            "averaging_period_status": period_status,
            "averaging_period": avg_summary,
            "is_cpcb_compliant": False,          # Strictly False: manual input is user-declared, not independently verified
            "verification_status": verification_status,
            "provenance": provenance,
            "reason": reason
        }
        
        return AQIResponse(**response_data)
        
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"ML prediction failed: {str(e)}")

@router.get("/city/{city_id}", response_model=List[AQIResponse])
async def get_city_aqi_records(
    city_id: str,
    db: Session = Depends(get_db)
):
    """Get latest 30 AQI records for a city with ML predictions"""
    
    try:
        city_uuid = uuid.UUID(city_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid city_id format")
    
    records = db.query(AirQualityData, MLPrediction)\
        .join(MLPrediction, AirQualityData.record_id == MLPrediction.record_id, isouter=True)\
        .filter(AirQualityData.city_id == city_uuid)\
        .order_by(desc(AirQualityData.recorded_at))\
        .limit(30)\
        .all()
    
    result = []
    for record, prediction in records:
        result.append(AQIResponse(
            record_id=record.record_id,
            city_id=city_id,
            pm25=record.pm25,
            pm10=record.pm10,
            no2=record.no2,
            co=record.co,
            so2=record.so2,
            o3=record.o3,
            aqi_score=record.aqi_score,
            recorded_at=record.recorded_at,
            risk_level=prediction.risk_level if prediction else None,
            calculation_type=record.calculation_type or "stored_record",
            calculation_formula=record.calculation_formula or "cpcb_formula",
            calculation_status=record.calculation_type or "stored_record",
            averaging_period_status=record.averaging_period_status or "unverified",
            averaging_period="unspecified",
            observation_time=record.observation_time,
            ingested_at=record.ingested_at,
            is_cpcb_compliant=record.is_cpcb_compliant if record.is_cpcb_compliant is not None else False,
            verification_status=record.verification_status or "unverified_stored_record",
            provenance=record.provenance or "database_history",
            data_source=record.data_source,
            averaging_periods=record.averaging_periods
        ))
    
    return result

@router.get("/cities")
async def get_all_cities_latest_aqi(db: Session = Depends(get_db)):
    """Get latest AQI for all cities for dashboard map"""
    
    subq = db.query(
        AirQualityData.city_id,
        func.max(AirQualityData.recorded_at).label('latest_date')
    ).group_by(AirQualityData.city_id).subquery()
    
    cities_data = db.query(
        City.city_id,
        City.city_name,
        AirQualityData.aqi_score,
        AirQualityData.pm25,
        AirQualityData.pm10,
        AirQualityData.no2,
        AirQualityData.o3,
        MLPrediction.risk_level
    ).join(
        AirQualityData, City.city_id == AirQualityData.city_id
    ).join(
        subq,
        (AirQualityData.city_id == subq.c.city_id) &
        (AirQualityData.recorded_at == subq.c.latest_date)
    ).outerjoin(
        MLPrediction, AirQualityData.record_id == MLPrediction.record_id
    ).all()

    result = []
    for city_id, city_name, aqi_score, pm25, pm10, no2, o3, risk_level in cities_data:
        result.append({
            "id": str(city_id),
            "city_id": str(city_id),
            "name": city_name,
            "city_name": city_name,
            "aqi_value": aqi_score,
            "aqi_score": aqi_score,
            "pm25": pm25,
            "pm10": pm10,
            "no2": no2,
            "o3": o3,
            "risk_level": risk_level,
            "state": None
        })

    return result

@router.get("/city/{city_id}/latest")
async def get_city_latest_aqi(city_id: str, db: Session = Depends(get_db)):
    """Get single latest AQI record for a city"""

    try:
        city_uuid = uuid.UUID(city_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid city_id format")

    city = db.query(City).filter(City.city_id == city_uuid).first()
    if not city:
        raise HTTPException(status_code=404, detail="City not found")

    latest_record = db.query(AirQualityData, MLPrediction)\
        .join(MLPrediction, AirQualityData.record_id == MLPrediction.record_id, isouter=True)\
        .filter(AirQualityData.city_id == city_uuid)\
        .order_by(desc(AirQualityData.recorded_at))\
        .first()
    
    aqi_row, ml_row = latest_record if latest_record else (None, None)

    live = await fetch_live_aqi(city.city_name)
    source_obs_dt = None
    if live and live.get("dt"):
        try:
            from datetime import timezone as _tz
            source_obs_dt = datetime.fromtimestamp(live["dt"], tz=_tz.utc).replace(tzinfo=None)
        except Exception:
            source_obs_dt = None

    if live and aqi_row:
        aqi_row.pm25 = live["pm25"]
        aqi_row.pm10 = live["pm10"]
        aqi_row.no2  = live["no2"]
        aqi_row.co   = live["co"]
        aqi_row.so2  = live["so2"]
        aqi_row.o3   = live["o3"]
        if source_obs_dt is not None:
            aqi_row.recorded_at = source_obs_dt
            aqi_row.observation_time = source_obs_dt
        aqi_row.ingested_at = datetime.now(timezone.utc)
        aqi_row.data_source = live.get("data_source", "openweather")
        aqi_row.calculation_type = live.get("calculation_type", "instantaneous_estimate")
        aqi_row.calculation_formula = live.get("calculation_formula", "cpcb_formula")
        aqi_row.is_cpcb_compliant = live.get("is_cpcb_compliant", False)
        aqi_row.verification_status = live.get("verification_status", "vendor_live_telemetry")
        aqi_row.averaging_period_status = live.get("averaging_period_status", "single_hour_instantaneous")
        aqi_row.averaging_periods = live.get("averaging_periods")
        aqi_row.provenance = live.get("provenance")
        db.commit()
        db.refresh(aqi_row)

    obs_time_iso = None
    if source_obs_dt is not None:
        obs_time_iso = source_obs_dt.isoformat()
    # When source_obs_dt is unavailable (fallback to stored DB record or live without dt),
    # do NOT fabricate or return recorded_at as source observation time! Return None!

    ingested_time_iso = live.get("ingested_at") if live else None

    return {
        "record_id": str(aqi_row.record_id) if aqi_row else None,
        "city_id": city_id,
        "city_name": city.city_name,
        "country": city.country,
        "aqi_score": aqi_row.aqi_score if aqi_row else None,
        "pm25": aqi_row.pm25 if aqi_row else None,
        "pm10": aqi_row.pm10 if aqi_row else None,
        "no2": aqi_row.no2 if aqi_row else None,
        "co": aqi_row.co if aqi_row else None,
        "so2": aqi_row.so2 if aqi_row else None,
        "o3": aqi_row.o3 if aqi_row else None,
        "co2_ppm": aqi_row.co2_ppm if aqi_row else None,
        "risk_level": ml_row.risk_level if ml_row else None,
        "recorded_at": aqi_row.recorded_at if aqi_row else None,
        "observation_time": obs_time_iso,
        "ingested_at": ingested_time_iso,
        "calculation_type": live.get("calculation_type") if live else "stored_record",
        "calculation_formula": "cpcb_formula",
        "is_cpcb_compliant": False,
        "cpcb_status": live.get("cpcb_status", "unverified") if live else "unverified",
        "verification_status": "vendor_live_telemetry" if live else "unverified_stored_record"
    }

@router.get("/history/{city_id}")
async def get_city_history(city_id: str, db: Session = Depends(get_db)):
    """Get 90 days history for chart (by city_id)"""
    try:
        city_uuid = uuid.UUID(city_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid city_id format")
    
    ninety_days_ago = datetime.now() - timedelta(days=90)
    
    history = db.query(
        func.date(AirQualityData.recorded_at).label('date'),
        func.avg(AirQualityData.aqi_score).label('avg_aqi')
    ).filter(
        AirQualityData.city_id == city_uuid,
        AirQualityData.recorded_at >= ninety_days_ago
    ).group_by(
        func.date(AirQualityData.recorded_at)
    ).order_by('date').all()
    
    return [{"date": d.isoformat(), "avg_aqi": float(a)} for d, a in history]

@router.get("/history_by_name/{city_name}")
async def get_city_history_by_name(city_name: str, days: int = 7, db: Session = Depends(get_db)):
    """Get historical AQI readings for a city by name for last N days (from AQIHistory)"""
    timeframe = datetime.now() - timedelta(days=days)
    history = db.query(AQIHistory).filter(
        AQIHistory.city_name == city_name,
        AQIHistory.recorded_at >= timeframe
    ).order_by(AQIHistory.recorded_at.asc()).all()
    
    return [{
        "recorded_at": r.recorded_at.isoformat() if r.recorded_at else None,
        "observation_time": r.observation_time.isoformat() if r.observation_time else None,
        "ingested_at": r.ingested_at.isoformat() if r.ingested_at else None,
        "aqi_score": r.aqi_score,
        "pm25": r.pm25,
        "pm10": r.pm10,
        "risk_level": r.risk_level,
        "data_source": r.data_source,
        "calculation_type": r.calculation_type,
        "is_cpcb_compliant": r.is_cpcb_compliant,
        "verification_status": r.verification_status,
        "provenance": r.provenance
    } for r in history]

@router.get("/live-status")
async def live_status():
    from config import get_settings
    key = get_settings().OPENWEATHER_API_KEY
    return {"live_api": bool(key), "message": "Live data active" if key else "Using cached seed data"}