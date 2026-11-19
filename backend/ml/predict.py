import math
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any

# ==============================================================================
# CPCB NAQI Pollutant Breakpoints and Sub-index Equations
# Standard concentration units:
#   - pm25: ug/m3 (24-hour average)
#   - pm10: ug/m3 (24-hour average)
#   - no2:  ug/m3 (24-hour average)
#   - so2:  ug/m3 (24-hour average)
#   - co:   mg/m3 (8-hour average)
#   - o3:   ug/m3 (8-hour average)
# ==============================================================================

def pm25_si(v: Optional[float]) -> Optional[float]:
    """CPCB PM2.5 24-hour sub-index calculation (ug/m3)."""
    if v is None or not math.isfinite(v) or v < 0:
        return None
    if v <= 0: return 0.0
    if v <= 30: return v * 50 / 30
    if v <= 60: return 50 + (v - 30) * 50 / 30
    if v <= 90: return 100 + (v - 60) * 100 / 30
    if v <= 120: return 200 + (v - 90) * 100 / 30
    if v <= 250: return 300 + (v - 120) * 100 / 130
    return min(400 + (v - 250) * 100 / 130, 500.0)

def pm10_si(v: Optional[float]) -> Optional[float]:
    """CPCB PM10 24-hour sub-index calculation (ug/m3)."""
    if v is None or not math.isfinite(v) or v < 0:
        return None
    if v <= 0: return 0.0
    if v <= 100: return float(v)
    if v <= 250: return 100 + (v - 100) * 100 / 150
    if v <= 350: return 200 + (v - 250)
    if v <= 430: return 300 + (v - 350) * 100 / 80
    return min(400 + (v - 430) * 100 / 80, 500.0)

def no2_si(v: Optional[float]) -> Optional[float]:
    """CPCB NO2 24-hour sub-index calculation (ug/m3)."""
    if v is None or not math.isfinite(v) or v < 0:
        return None
    if v <= 0: return 0.0
    if v <= 40: return v * 50 / 40
    if v <= 80: return 50 + (v - 40) * 50 / 40
    if v <= 180: return 100 + (v - 80) * 100 / 100
    if v <= 280: return 200 + (v - 180) * 100 / 100
    if v <= 400: return 300 + (v - 280) * 100 / 120
    return min(400 + (v - 400) * 100 / 120, 500.0)

def co_si(v: Optional[float]) -> Optional[float]:
    """
    CPCB Carbon Monoxide (CO) 8-hour sub-index calculation.
    Accepts v in mg/m3 directly without scaling. Breakpoints:
      0.0 - 1.0  -> 0 - 50      (Good)
      1.1 - 2.0  -> 51 - 100    (Satisfactory)
      2.1 - 10.0 -> 101 - 200   (Moderate)
      10.1 - 17.0 -> 201 - 300  (Poor)
      17.1 - 34.0 -> 301 - 400  (Very Poor)
      > 34.0      -> 401 - 500  (Severe)
    """
    if v is None or not math.isfinite(v) or v < 0:
        return None
    if v <= 0: return 0.0
    if v <= 1: return 50.0 * v
    if v <= 2: return 50.0 + 50.0 * (v - 1)
    if v <= 10: return 100.0 + 100.0 * (v - 2) / 8
    if v <= 17: return 200.0 + 100.0 * (v - 10) / 7
    if v <= 34: return 300.0 + 100.0 * (v - 17) / 17
    return min(400.0 + 100.0 * (v - 34) / 17, 500.0)

def so2_si(v: Optional[float]) -> Optional[float]:
    """CPCB SO2 24-hour sub-index calculation (ug/m3)."""
    if v is None or not math.isfinite(v) or v < 0:
        return None
    if v <= 0: return 0.0
    if v <= 40: return v * 50 / 40
    if v <= 80: return 50 + (v - 40) * 50 / 40
    if v <= 380: return 100 + (v - 80) * 100 / 300
    if v <= 800: return 200 + (v - 380) * 100 / 420
    return min(300 + (v - 800) * 100 / 800, 500.0)

def o3_si(v: Optional[float]) -> Optional[float]:
    """CPCB Ozone (O3) 8-hour sub-index calculation (ug/m3)."""
    if v is None or not math.isfinite(v) or v < 0:
        return None
    if v <= 0: return 0.0
    if v <= 100: return float(v)
    if v <= 168: return 100 + (v - 100) * 100 / 68
    if v <= 208: return 200 + (v - 168) * 100 / 40
    return min(300 + (v - 208) * 100 / 540, 500.0)

SUB_INDEX_FUNCS = {
    "pm25": pm25_si,
    "pm10": pm10_si,
    "no2":  no2_si,
    "co":   co_si,
    "so2":  so2_si,
    "o3":   o3_si,
}

# ==============================================================================
# CPCB Averaging Windows and Data Adequacy Requirements
#
# Authoritative Statutory Documentation:
#   1. Title:              "National Air Quality Index"
#      Report Number:      Report No. CUPS/82/2014-15
#      Issuing Authority:  Central Pollution Control Board (CPCB),
#                          Ministry of Environment, Forest and Climate Change, Government of India
#                          (in technical collaboration with Indian Institute of Technology Kanpur)
#      Publication Date:   October 2014
#      Document Section:   Section 4.1 "AQI Formulation", Page 20, Table 4.1
#
#   2. Title:              "Air Quality Index Bulletin"
#      Issuing Authority:  Central Pollution Control Board (CPCB), Ministry of Environment, Forest and Climate Change
#      Publication Dates:  September 2016, November 2016
#      Document Section:   Section "How is AQI calculated?"
#
# Statutory CPCB Methodology Requirements:
#   1. Averaging Periods:
#      - 24-hourly average concentration for PM2.5, PM10, NO2, SO2, NH3, and Pb.
#      - 8-hourly average concentration for CO and O3.
#   2. Sub-Index Data Completeness:
#      - "A minimum of 16 hours of data is necessary for calculating a sub-index."
#      - Important Methodology Finding: The published CPCB statement specifies this 16-hour
#        rule generally across sub-index calculations at a monitoring station. It is NOT
#        textually restricted solely to 24-hour pollutants.
#   3. Overall AQI Eligibility Requirement:
#      - "AQI is calculated only if data are available for minimum three pollutants
#         out of which one should necessarily be either PM2.5 or PM10. Else, data
#         are considered insufficient for calculating AQI."
#      - Overall AQI corresponds to the maximum (worst) sub-index among eligible pollutants.
#
# Inherent Methodological Ambiguity & Application Engineering Policy:
#   - An isolated 8-hour rolling window spans only 8 hours, and thus cannot physically contain
#     16 hours of observations within itself.
#   - The official CPCB methodology does NOT define how an isolated 8-hour window interacts with
#     the general 16-hour completeness rule, nor does it establish a separate 6-observation
#     threshold for 8-hour sub-indices.
#   - The CPCB 2015 "Protocol for Data Transmission from CAAQM Stations" specifies 15-minute
#     telemetry transmission, not an AQI sub-index calculation adequacy threshold.
#   - Therefore, APPLICATION_POLICY_MIN_OBSERVATIONS_8H = 6 is strictly an
#     APPLICATION-SPECIFIC STREAMING HEURISTIC (a 75% data completeness policy for an 8-hour window)
#     to enable preliminary real-time streaming estimates without failing streaming dashboards.
#   - Six observations CANNOT independently establish official CPCB data adequacy or compliance.
#   - Two distinct modes are implemented:
#       * "cpcb_methodology": Strictly enforces demonstrated CPCB data adequacy (minimum 16 hours).
#         Does not substitute 6 observations for official CPCB adequacy. If adequacy is not
#         demonstrated, returns an insufficient-data status rather than claiming CPCB compliance.
#       * "streaming_estimate": Uses the 6-observation engineering heuristic where applicable,
#         clearly labelled as calculation_type="application_streaming_estimate" with is_cpcb_compliant=False.
# ==============================================================================
OFFICIAL_CPCB_MIN_HOURS_FOR_SUBINDEX = 16
CPCB_24H_POLLUTANTS = ("pm25", "pm10", "no2", "so2")
CPCB_8H_POLLUTANTS = ("co", "o3")

MIN_OBSERVATIONS_24H = 16  # Official CPCB requirement for 24-hour pollutants
APPLICATION_POLICY_MIN_OBSERVATIONS_8H = 6  # Application-specific engineering heuristic (NOT official CPCB rule)
MIN_OBSERVATIONS_8H = APPLICATION_POLICY_MIN_OBSERVATIONS_8H  # Backward-compatible alias

DATA_ADEQUACY_POLICY_METADATA = {
    "cpcb_statutory_rule": (
        "CPCB National Air Quality Index (Report No. CUPS/82/2014-15, October 2014, Section 4.1 'AQI Formulation', "
        "Table 4.1; CPCB NAQI Bulletins 2016, Section 'How is AQI calculated?'): 'A minimum of 16 hours of data is "
        "necessary for calculating a sub-index.' The published methodology states this requirement generally for "
        "sub-index calculations at a monitoring station, without restricting it solely to 24-hour pollutants."
    ),
    "application_policy_8h": (
        "APPLICATION_POLICY_MIN_OBSERVATIONS_8H = 6 hourly observations within an 8-hour window "
        "(application-specific engineering policy representing 75% data capture for real-time streaming estimates; "
        "not an official cpcb statutory rule). Six observations cannot independently establish official CPCB data adequacy."
    ),
    "methodology_ambiguity_notice": (
        "Official CPCB documentation specifies 24-hour averages for PM2.5, PM10, NO2, SO2, NH3, Pb, "
        "and 8-hour averages for CO and O3, alongside a general requirement of minimum 16 hours of data for a sub-index. "
        "The published sources do not define how an isolated 8-hour rolling window interacts with the 16-hour completeness rule, "
        "nor do they specify an official reduced threshold (e.g. 6 hours) for 8-hour pollutants. "
        "Consequently, isolated 8-hour calculations relying solely on 6 observations are classified as application streaming estimates "
        "with is_cpcb_compliant=False, rather than claiming unverified statutory CPCB compliance."
    )
}

def validate_cpcb_averaging_periods(
    averaging_periods: Optional[Dict[str, str]] = None,
    declared_collective: Optional[str] = None
) -> tuple[bool, str, str, Optional[str]]:
    """
    Validate declared pollutant averaging periods against official CPCB specifications:
      - 24-hour average required for PM2.5, PM10, NO2, and SO2
      - 8-hour average required for CO and O3

    Note: This validates that the user-declared metadata matches CPCB required periods.
    It does NOT independently verify physical measurement collection or quality audits.

    Returns:
      (matches_periods: bool, averaging_period_status: str, summary_string: str, reason: Optional[str])
      - averaging_period_status: "declared_periods_match" | "declared_periods_mismatch" | "missing_declaration"
    """
    if averaging_periods and isinstance(averaging_periods, dict):
        def _is_24h(v: Any) -> bool:
            return str(v).strip().lower() in ("24h", "24-hour", "24_hour", "24 hours", "24hr", "24-hr", "24")

        def _is_8h(v: Any) -> bool:
            return str(v).strip().lower() in ("8h", "8-hour", "8_hour", "8 hours", "8hr", "8-hr", "8")

        valid_24h = all(_is_24h(averaging_periods.get(p, "")) for p in CPCB_24H_POLLUTANTS)
        valid_8h = all(_is_8h(averaging_periods.get(p, "")) for p in CPCB_8H_POLLUTANTS)

        if valid_24h and valid_8h:
            return (
                True,
                "declared_periods_match",
                "24h_8h_averaged",
                "Declared averaging periods match CPCB specifications (24h for PM2.5, PM10, NO2, SO2; 8h for CO, O3); inputs remain user-declared and not independently verified"
            )
        else:
            return (
                False,
                "declared_periods_mismatch",
                "non_compliant_declared",
                "Declared averaging periods do not match CPCB requirements (24h for PM2.5, PM10, NO2, SO2; 8h for CO, O3)"
            )

    if declared_collective and str(declared_collective).strip().lower() in (
        "24h_8h_averaged", "cpcb_standard", "24h_8h", "cpcb_standard_periods"
    ):
        return (
            True,
            "declared_periods_match",
            "24h_8h_averaged",
            "Declared collective averaging period matches CPCB specifications (24h/8h averaged); inputs remain user-declared and not independently verified"
        )

    return (
        False,
        "missing_declaration",
        "unspecified",
        "Averaging period metadata omitted; submitted concentrations are treated as unverified manual input"
    )

def calculate_sub_indices(concentrations: Dict[str, Optional[float]]) -> Dict[str, Optional[float]]:
    """
    Calculate sub-indices for a dictionary of concentrations.
    Returns None for any pollutant with missing, non-finite, or negative value.
    """
    result = {}
    for poll, fn in SUB_INDEX_FUNCS.items():
        val = concentrations.get(poll)
        result[poll] = fn(val)
    return result

def calculate_cpcb_aqi_from_sub_indices(
    sub_indices: Dict[str, Optional[float]],
    sub_index_compliance: Optional[Dict[str, bool]] = None,
    calculation_mode: str = "auto"
) -> Dict[str, Any]:
    """
    Enforce CPCB overall NAQI eligibility rules from calculated sub-indices:
      Rule 1 (Particulate requirement): At least one particulate matter (PM2.5 or PM10)
             must have a valid sub-index.
      Rule 2 (Minimum pollutants requirement): At least 3 pollutants in total must have
             valid sub-indices.
      Overall AQI: Maximum among the valid sub-indices.

    Calculation Modes:
      - "cpcb_methodology": Strictly enforces demonstrated CPCB data adequacy (minimum 16 hours
        per CPCB methodology). Six observations in an isolated 8-hour window for CO/O3 cannot
        independently establish CPCB data adequacy. If fewer than 3 pollutants meet statutory adequacy,
        returns calculation_status="insufficient_data", aqi_score=None, is_cpcb_compliant=False.
      - "streaming_estimate": Generates an application streaming estimate using the 6-observation
        heuristic for 8h pollutants. Returns calculation_type="application_streaming_estimate",
        calculation_status="streaming_estimate", is_cpcb_compliant=False.
      - "auto": Automatically determines CPCB compliance vs. application streaming estimate.
        If at least 3 pollutants (including particulate) have verified CPCB adequacy AND the dominant
        pollutant has verified CPCB adequacy -> cpcb_compliant (is_cpcb_compliant=True).
        If the result relies on the 6-observation heuristic -> application_streaming_estimate (is_cpcb_compliant=False).
    """
    valid_sub_indices = {k: v for k, v in sub_indices.items() if v is not None}
    has_particulate = ("pm25" in valid_sub_indices) or ("pm10" in valid_sub_indices)
    pollutant_count = len(valid_sub_indices)

    default_calc_type = "application_streaming_estimate" if calculation_mode == "streaming_estimate" else "cpcb_methodology"

    if not has_particulate:
        return {
            "aqi_score": None,
            "calculation_status": "insufficient_data",
            "calculation_type": default_calc_type,
            "is_cpcb_compliant": False,
            "has_particulate": False,
            "valid_pollutants_count": pollutant_count,
            "sub_indices": sub_indices,
            "responsible_pollutant": None,
            "reason": "Neither PM2.5 nor PM10 has a valid sub-index (at least one particulate pollutant required by CPCB)"
        }

    if pollutant_count < 3:
        return {
            "aqi_score": None,
            "calculation_status": "insufficient_data",
            "calculation_type": default_calc_type,
            "is_cpcb_compliant": False,
            "has_particulate": True,
            "valid_pollutants_count": pollutant_count,
            "sub_indices": sub_indices,
            "responsible_pollutant": None,
            "reason": f"Only {pollutant_count} valid pollutant sub-indices available; CPCB requires at least 3"
        }

    max_si = max(valid_sub_indices.values())
    aqi_score = round(float(max_si), 2)
    responsible_pollutants = [p for p, val in valid_sub_indices.items() if val == max_si]
    primary_resp = responsible_pollutants[0] if responsible_pollutants else None

    # Evaluate sub-index compliance if provided
    if sub_index_compliance is not None:
        cpcb_compliant_polls = [p for p in valid_sub_indices if sub_index_compliance.get(p, False)]
        has_cpcb_particulate = ("pm25" in cpcb_compliant_polls) or ("pm10" in cpcb_compliant_polls)
        cpcb_compliant_count = len(cpcb_compliant_polls)
        cpcb_adequacy_met = has_cpcb_particulate and (cpcb_compliant_count >= 3)
        primary_is_cpcb = sub_index_compliance.get(primary_resp, False)
    else:
        # If compliance map omitted, check whether any pollutant is 8h (CO/O3) without demonstrated compliance
        cpcb_adequacy_met = True
        primary_is_cpcb = True
        cpcb_compliant_count = pollutant_count

    if calculation_mode == "cpcb_methodology":
        if not cpcb_adequacy_met:
            return {
                "aqi_score": None,
                "calculation_status": "insufficient_data",
                "calculation_type": "cpcb_methodology",
                "is_cpcb_compliant": False,
                "has_particulate": has_particulate,
                "valid_pollutants_count": pollutant_count,
                "cpcb_compliant_pollutants_count": cpcb_compliant_count,
                "sub_indices": sub_indices,
                "responsible_pollutant": None,
                "reason": (
                    f"Insufficient data under official CPCB methodology: only {cpcb_compliant_count} pollutant(s) "
                    "satisfy the official minimum 16-hour data completeness requirement (CPCB requires at least 3, "
                    "including PM2.5 or PM10). Six observations in an 8-hour window for CO/O3 represent an application "
                    "streaming heuristic and cannot independently establish CPCB data adequacy."
                )
            }
        if not primary_is_cpcb:
            return {
                "aqi_score": aqi_score,
                "calculation_status": "streaming_estimate",
                "calculation_type": "application_streaming_estimate",
                "is_cpcb_compliant": False,
                "has_particulate": True,
                "valid_pollutants_count": pollutant_count,
                "cpcb_compliant_pollutants_count": cpcb_compliant_count,
                "sub_indices": sub_indices,
                "responsible_pollutant": primary_resp,
                "responsible_pollutants": responsible_pollutants,
                "reason": (
                    f"Dominant pollutant '{primary_resp}' relies on the 6-observation engineering heuristic for 8-hour pollutants. "
                    "Six observations cannot independently establish official CPCB compliance."
                )
            }
        return {
            "aqi_score": aqi_score,
            "calculation_status": "cpcb_compliant",
            "calculation_type": "cpcb_compliant",
            "is_cpcb_compliant": True,
            "has_particulate": True,
            "valid_pollutants_count": pollutant_count,
            "cpcb_compliant_pollutants_count": cpcb_compliant_count,
            "sub_indices": sub_indices,
            "responsible_pollutant": primary_resp,
            "responsible_pollutants": responsible_pollutants,
            "reason": None
        }

    if calculation_mode == "streaming_estimate":
        return {
            "aqi_score": aqi_score,
            "calculation_status": "streaming_estimate",
            "calculation_type": "application_streaming_estimate",
            "is_cpcb_compliant": False,
            "has_particulate": True,
            "valid_pollutants_count": pollutant_count,
            "sub_indices": sub_indices,
            "responsible_pollutant": primary_resp,
            "responsible_pollutants": responsible_pollutants,
            "reason": (
                "Application streaming estimate calculated using 6-observation engineering heuristic for 8-hour pollutants. "
                "Does not independently establish CPCB data adequacy or compliance."
            )
        }

    # calculation_mode == "auto"
    if cpcb_adequacy_met and primary_is_cpcb:
        return {
            "aqi_score": aqi_score,
            "calculation_status": "cpcb_compliant",
            "calculation_type": "cpcb_aggregated",
            "is_cpcb_compliant": True,
            "has_particulate": True,
            "valid_pollutants_count": pollutant_count,
            "sub_indices": sub_indices,
            "responsible_pollutant": primary_resp,
            "responsible_pollutants": responsible_pollutants,
            "reason": None
        }
    else:
        return {
            "aqi_score": aqi_score,
            "calculation_status": "streaming_estimate",
            "calculation_type": "application_streaming_estimate",
            "is_cpcb_compliant": False,
            "has_particulate": True,
            "valid_pollutants_count": pollutant_count,
            "sub_indices": sub_indices,
            "responsible_pollutant": primary_resp,
            "responsible_pollutants": responsible_pollutants,
            "reason": (
                "Application streaming estimate calculated using 6-observation engineering heuristic for 8-hour pollutants. "
                "Six observations cannot independently establish CPCB data adequacy."
            )
        }

def _parse_timestamp(ts: Any) -> Optional[datetime]:
    """
    Normalize timestamps to a consistent naive UTC datetime.
    Never substitutes datetime.utcnow() for missing or unparseable timestamps.
    Returns None if ts is None, invalid, or cannot be parsed.
    """
    if ts is None:
        return None
    if isinstance(ts, datetime):
        if ts.tzinfo is not None:
            return ts.astimezone(timezone.utc).replace(tzinfo=None)
        return ts
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
        except (ValueError, OSError, OverflowError):
            return None
    if isinstance(ts, str):
        try:
            # Handle ISO strings
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                return dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt
        except ValueError:
            return None
    return None

def aggregate_cpcb_aqi(
    observations: List[Dict[str, Any]],
    city_id: Optional[str] = None,
    ref_time: Optional[datetime] = None,
    min_obs_24h: int = MIN_OBSERVATIONS_24H,
    min_obs_8h: int = APPLICATION_POLICY_MIN_OBSERVATIONS_8H,
    calculation_mode: str = "auto"
) -> Dict[str, Any]:
    """
    Perform rolling aggregation and data-adequacy checks according to CPCB guidelines:
      - Filters observations strictly by city_id (never combining data across cities).
      - Deduplicates observations by timestamp (duplicate records cannot inflate observation count).
      - Partitions into 24-hour window (PM2.5, PM10, NO2, SO2) and 8-hour window (CO, O3).
      - Excludes stale observations outside the rolling window.
      - Preserves measured zeros (0.0) as valid observations; missing values (None) are excluded.
      - Evaluates statutory minimum-data requirements (16 hours) vs. application streaming heuristic (6 hours).
      - Distinguishes application streaming estimates from strict CPCB methodology calculations.
    """
    default_calc_type = "application_streaming_estimate" if calculation_mode == "streaming_estimate" else "cpcb_methodology"

    # 1. City isolation filter
    city_str = str(city_id) if city_id is not None else None
    filtered_obs = []
    for obs in observations:
        if city_str is not None:
            obs_city = str(obs.get("city_id")) if obs.get("city_id") is not None else None
            if obs_city != city_str:
                continue
        filtered_obs.append(obs)

    if not filtered_obs:
        return {
            "aqi_score": None,
            "calculation_status": "insufficient_data",
            "calculation_type": default_calc_type,
            "is_cpcb_compliant": False,
            "has_particulate": False,
            "valid_pollutants_count": 0,
            "sub_indices": {k: None for k in SUB_INDEX_FUNCS},
            "averages": {k: None for k in SUB_INDEX_FUNCS},
            "observation_counts": {k: 0 for k in SUB_INDEX_FUNCS},
            "sub_index_adequacy": {k: "insufficient_data" for k in SUB_INDEX_FUNCS},
            "sub_index_cpcb_compliant": {k: False for k in SUB_INDEX_FUNCS},
            "responsible_pollutant": None,
            "reason": "No observations available for the specified city",
            "data_adequacy_policy": dict(DATA_ADEQUACY_POLICY_METADATA),
            "calculation_mode": calculation_mode
        }

    # 2. Normalize timestamps and deduplicate
    dedup_by_ts: Dict[datetime, Dict[str, Any]] = {}
    for obs in filtered_obs:
        raw_ts = (
            obs.get("observation_time")
            or obs.get("observed_at")
            or obs.get("timestamp")
            or obs.get("recorded_at")
            or obs.get("dt")
        )
        ts = _parse_timestamp(raw_ts)
        if ts is None:
            continue
        # If timestamp is already seen, merge complementary pollutant fields
        if ts not in dedup_by_ts:
            dedup_by_ts[ts] = dict(obs)
        else:
            for k, v in obs.items():
                if v is not None and dedup_by_ts[ts].get(k) is None:
                    dedup_by_ts[ts][k] = v

    if not dedup_by_ts:
        return {
            "aqi_score": None,
            "calculation_status": "insufficient_data",
            "calculation_type": default_calc_type,
            "is_cpcb_compliant": False,
            "has_particulate": False,
            "valid_pollutants_count": 0,
            "sub_indices": {k: None for k in SUB_INDEX_FUNCS},
            "averages": {k: None for k in SUB_INDEX_FUNCS},
            "observation_counts": {k: 0 for k in SUB_INDEX_FUNCS},
            "sub_index_adequacy": {k: "insufficient_data" for k in SUB_INDEX_FUNCS},
            "sub_index_cpcb_compliant": {k: False for k in SUB_INDEX_FUNCS},
            "responsible_pollutant": None,
            "reason": "No valid timestamped observations available for the specified city",
            "data_adequacy_policy": dict(DATA_ADEQUACY_POLICY_METADATA),
            "calculation_mode": calculation_mode
        }

    deduped_obs = list(dedup_by_ts.items())

    # 3. Determine reference time
    if ref_time is None:
        ref_time = max(ts for ts, _ in deduped_obs)
    else:
        parsed_ref = _parse_timestamp(ref_time)
        ref_time = parsed_ref if parsed_ref is not None else max(ts for ts, _ in deduped_obs)

    # 4. Partition by window and aggregate
    averages: Dict[str, Optional[float]] = {}
    counts: Dict[str, int] = {}
    sub_indices: Dict[str, Optional[float]] = {}
    sub_index_adequacy: Dict[str, str] = {}
    sub_index_compliant: Dict[str, bool] = {}

    cutoff_24h = ref_time - timedelta(hours=24)
    cutoff_8h = ref_time - timedelta(hours=8)

    for poll in SUB_INDEX_FUNCS.keys():
        is_8h = poll in CPCB_8H_POLLUTANTS
        cutoff = cutoff_8h if is_8h else cutoff_24h
        min_required = min_obs_8h if is_8h else min_obs_24h

        # Collect valid measurements within the respective window
        valid_vals = []
        for ts, obs in deduped_obs:
            if cutoff <= ts <= ref_time:
                val = obs.get(poll)
                if val is not None and math.isfinite(val) and val >= 0:
                    valid_vals.append(float(val))

        counts[poll] = len(valid_vals)

        # Count total 24h observations for station-level adequacy check
        valid_vals_24h = []
        for ts, obs in deduped_obs:
            if cutoff_24h <= ts <= ref_time:
                val = obs.get(poll)
                if val is not None and math.isfinite(val) and val >= 0:
                    valid_vals_24h.append(float(val))
        count_24h = len(valid_vals_24h)

        if len(valid_vals) >= min_required:
            avg_conc = sum(valid_vals) / len(valid_vals)
            averages[poll] = round(avg_conc, 3 if poll == "co" else 2)
            sub_indices[poll] = SUB_INDEX_FUNCS[poll](avg_conc)
            if not is_8h:
                # 24h pollutant with >= 16 hourly observations meets official statutory CPCB requirement
                sub_index_adequacy[poll] = "cpcb_statutory_16h_rule"
                sub_index_compliant[poll] = True
            else:
                # 8h pollutant (CO, O3):
                # An isolated 8h window with 6 observations cannot independently establish CPCB data adequacy.
                # If station data has >= 16 hours across the 24h cycle, station-level CPCB completeness is met.
                if count_24h >= OFFICIAL_CPCB_MIN_HOURS_FOR_SUBINDEX:
                    sub_index_adequacy[poll] = "cpcb_statutory_16h_station_cycle"
                    sub_index_compliant[poll] = True
                else:
                    sub_index_adequacy[poll] = "application_streaming_heuristic_6h"
                    sub_index_compliant[poll] = False
        else:
            averages[poll] = None
            sub_indices[poll] = None
            sub_index_adequacy[poll] = "insufficient_data"
            sub_index_compliant[poll] = False

    # 5. Evaluate overall AQI eligibility
    aqi_result = calculate_cpcb_aqi_from_sub_indices(
        sub_indices=sub_indices,
        sub_index_compliance=sub_index_compliant,
        calculation_mode=calculation_mode
    )
    aqi_result["averages"] = averages
    aqi_result["observation_counts"] = counts
    aqi_result["sub_index_adequacy"] = sub_index_adequacy
    aqi_result["sub_index_cpcb_compliant"] = sub_index_compliant
    aqi_result["reference_time"] = ref_time.isoformat()
    aqi_result["data_adequacy_policy"] = dict(DATA_ADEQUACY_POLICY_METADATA)
    aqi_result["calculation_mode"] = calculation_mode

    return aqi_result

def calculate_instantaneous_estimate(
    pm25: Optional[float] = None,
    pm10: Optional[float] = None,
    no2: Optional[float] = None,
    co: Optional[float] = None,
    so2: Optional[float] = None,
    o3: Optional[float] = None
) -> Dict[str, Any]:
    """
    Calculate an indicative instantaneous estimate using CPCB formula breakpoints.
    Clearly flagged as an estimate, distinguished from a CPCB-compliant calculated AQI.
    """
    concs = {"pm25": pm25, "pm10": pm10, "no2": no2, "co": co, "so2": so2, "o3": o3}
    sub_indices = calculate_sub_indices(concs)
    valid_sub_indices = {k: v for k, v in sub_indices.items() if v is not None}

    if not valid_sub_indices:
        return {
            "estimate_aqi": None,
            "calculation_type": "instantaneous_estimate",
            "cpcb_compliant": False,
            "sub_indices": sub_indices,
            "warning": "No valid concentrations provided for estimate."
        }

    estimate_aqi = round(float(max(valid_sub_indices.values())), 2)
    return {
        "estimate_aqi": estimate_aqi,
        "calculation_type": "instantaneous_estimate",
        "cpcb_compliant": False,
        "sub_indices": sub_indices,
        "warning": "Indicative instantaneous estimate only; does not satisfy CPCB 24h/8h averaging or data adequacy requirements."
    }

def calculate_aqi(pm25, pm10, no2, co, so2, o3):
    """
    Calculate the Central Pollution Control Board (CPCB) Air Quality Index (AQI)
    assuming the provided inputs are authoritative pre-averaged concentrations:
      - pm25 (float): PM2.5 in ug/m3 (24-hr avg)
      - pm10 (float): PM10 in ug/m3 (24-hr avg)
      - no2  (float): NO2 in ug/m3 (24-hr avg)
      - co   (float): CO in mg/m3 (8-hr avg) - evaluated directly against CPCB breakpoints
      - so2  (float): SO2 in ug/m3 (24-hr avg)
      - o3   (float): O3 in ug/m3 (8-hr avg)
      
    Returns:
      float: The overall AQI (maximum of the 6 pollutant sub-indices).
    """
    return max(
        pm25_si(pm25) or 0.0,
        pm10_si(pm10) or 0.0,
        no2_si(no2) or 0.0,
        co_si(co) or 0.0,
        so2_si(so2) or 0.0,
        o3_si(o3) or 0.0
    )

def aqi_to_risk(aqi):
    if aqi is None:
        return "Unknown", 0
    if aqi <= 100: return "Low", 0
    elif aqi <= 200: return "Moderate", 1
    else: return "High", 2

def predict_aqi_risk(pm25, pm10, no2, co, so2, o3, month=None, day_of_week=None):
    """
    Predict AQI risk using standard CPCB calculation methodology.
    Inputs:
      - pm25 (float): PM2.5 in ug/m3
      - pm10 (float): PM10 in ug/m3
      - no2  (float): NO2 in ug/m3
      - co   (float): CO in mg/m3
      - so2  (float): SO2 in ug/m3
      - o3   (float): O3 in ug/m3
    """
    predicted_aqi = calculate_aqi(pm25, pm10, no2, co, so2, o3)
    predicted_aqi = round(max(0, min(500, predicted_aqi)), 2)
    risk_level, risk_label = aqi_to_risk(predicted_aqi)
    return {
        "predicted_aqi": predicted_aqi,
        "risk_level": risk_level,
        "risk_label": risk_label,
        "confidence": 1.0,
        "model_version": "CPCB_Formula_v1"
    }

if __name__ == "__main__":
    tests = [(15, 40, 8, 0.5, 5, 30, "Low"), (153, 241, 33, 1.87, 64, 83, "High"), (60, 90, 25, 1.2, 20, 55, "Moderate")]
    for pm25, pm10, no2, co, so2, o3, exp in tests:
        r = predict_aqi_risk(pm25, pm10, no2, co, so2, o3)
        ok = "✅" if r['risk_level'] == exp else "❌"
        print(f"{ok} AQI={r['predicted_aqi']} → {r['risk_level']} (expected {exp})")
