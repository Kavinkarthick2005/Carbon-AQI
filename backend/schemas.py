import math
from pydantic import BaseModel, ConfigDict, field_validator
from typing import Optional, Dict, Any, Union
from datetime import datetime
from uuid import UUID

# AQI Schemas
class AQIRecord(BaseModel):
    """
    AQI data ingestion schema adhering to the standard CPCB concentration unit contract:
      - city_id: UUID string referencing an existing city
      - pm25: float in ug/m3 (24-hour average)
      - pm10: float in ug/m3 (24-hour average)
      - no2: float in ug/m3 (24-hour average)
      - co: float in mg/m3 (8-hour average) - evaluated directly without scaling
      - so2: float in ug/m3 (24-hour average)
      - o3: float in ug/m3 (8-hour average)
      - co2_ppm: optional float in ppm (ambient CO2, tracked separately from CO)
      - aqi_score: optional float (ignored in favor of server calculation)
      - observation_time: optional datetime of source measurement/observation
      - averaging_periods: optional dict associating each pollutant with declared averaging window
          (e.g., {"pm25": "24h", "pm10": "24h", "no2": "24h", "so2": "24h", "co": "8h", "o3": "8h"})
      - declared_averaging_period: optional collective declaration (e.g. "24h_8h_averaged")
      - source_provenance: optional declared source/station/device label
      
    All 6 criteria pollutants are strictly required. Missing values are rejected (HTTP 422).
    Explicit zero measurements (0.0) are accepted as valid zero concentrations.
    """
    city_id: str
    pm25: float
    pm10: float
    no2: float
    co: float
    so2: float
    o3: float
    co2_ppm: Optional[float] = None
    aqi_score: Optional[float] = None
    observation_time: Optional[datetime] = None
    averaging_periods: Optional[Dict[str, str]] = None
    declared_averaging_period: Optional[str] = None
    source_provenance: Optional[str] = None

    @field_validator("pm25", "pm10", "no2", "co", "so2", "o3", "co2_ppm", "aqi_score")
    @classmethod
    def validate_finite_and_non_negative(cls, v: Optional[float]) -> Optional[float]:
        if v is None:
            return v
        if not math.isfinite(v):
            raise ValueError("Pollutant and AQI values must be finite numbers")
        if v < 0:
            raise ValueError("Pollutant and AQI values cannot be negative")
        return v

class AQIResponse(BaseModel):
    """
    Response model for AQI records.
    Pollutant concentrations are returned in standard CPCB units:
      - pm25, pm10, no2, so2, o3: ug/m3
      - co: mg/m3
      - aqi_score: authoritative server-calculated CPCB AQI score
      - recorded_at: database persisted timestamp
      - observation_time: source observation timestamp when known, else None
      - ingested_at: application ingestion timestamp when known, else None
      - calculation_type: methodology string (e.g. manual_submission, instantaneous_estimate, stored_record)
      - calculation_formula: formula used (e.g. cpcb_formula)
      - calculation_status: calculation status (e.g. valid, unverified, insufficient_data, invalid_averaging_period)
      - averaging_period_status: declared period match status (e.g. declared_periods_match, missing_declaration, declared_periods_mismatch)
      - averaging_period: regulatory averaging window representation (e.g. 24h_8h_averaged, unspecified, non_compliant_declared)
      - is_cpcb_compliant: boolean indicating verified CPCB compliance; strictly False for manual submissions without independent sensor audit
      - verification_status: provenance declaration status (e.g. user_declared_admin, unverified_user_declaration, unverified_stored_record)
      - provenance: source description or declaring identity
      - reason: optional explanation when compliance or adequacy criteria are not met
    """
    model_config = ConfigDict(from_attributes=True)
    
    record_id: UUID
    city_id: str
    pm25: float
    pm10: float
    no2: float
    co: Optional[float] = None
    so2: Optional[float] = None
    o3: float
    aqi_score: float
    recorded_at: datetime
    observation_time: Optional[datetime] = None
    ingested_at: Optional[datetime] = None
    risk_level: Optional[str] = None
    calculation_type: Optional[str] = "manual_submission"
    calculation_formula: Optional[str] = "cpcb_formula"
    calculation_status: Optional[str] = "valid"
    averaging_period_status: Optional[str] = "unspecified"
    averaging_period: Optional[str] = "unspecified"
    is_cpcb_compliant: Optional[bool] = False
    verification_status: Optional[str] = "user_declared_admin"
    provenance: Optional[Union[Dict[str, Any], str]] = None
    data_source: Optional[str] = None
    averaging_periods: Optional[Dict[str, Any]] = None
    reason: Optional[str] = None

# Carbon Footprint Schemas
class CarbonInput(BaseModel):
    transport_km: float
    transport_type: str  # car/bus/bike/train
    energy_kwh: float
    diet_type: str  # veg/non-veg/vegan

class CarbonResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    
    footprint_id: UUID
    transport_emission: float
    energy_emission: float
    diet_emission: float
    total_emission: float

# ML Prediction Schemas
class MLPredictionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    
    prediction_id: UUID
    record_id: UUID
    predicted_aqi: float
    risk_level: str
    model_version: str

# RL Strategy Schemas
class StrategyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    
    strategy_id: UUID
    strategy_name: str
    description: str
    rl_reward: float

# Chatbot Schemas
class ChatbotRequest(BaseModel):
    message: str
    language: str = 'en'

class ChatbotResponse(BaseModel):
    response: str
    language: str

# Feedback Schemas
class FeedbackCreate(BaseModel):
    subject: str
    message: str

class FeedbackResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    
    feedback_id: UUID
    subject: str
    status: str
    created_at: datetime

# User Schemas
class UserCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: Optional[str] = None
    display_name: Optional[str] = None

class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    
    user_id: UUID
    email: str
    name: str
    role: str
    preferred_language: str

# City Schemas
class CityResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    
    city_id: str
    city_name: str
    country: str
    latitude: float
    longitude: float

# Comparison Schemas
class ComparisonRequest(BaseModel):
    city1_id: str
    city2_id: str
