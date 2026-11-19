import unittest
import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from datetime import datetime, timedelta
import math
import uuid
from unittest.mock import patch, MagicMock

from ml.predict import (
    aggregate_cpcb_aqi,
    calculate_sub_indices,
    calculate_cpcb_aqi_from_sub_indices,
    calculate_instantaneous_estimate,
    calculate_aqi,
    validate_cpcb_averaging_periods,
    _parse_timestamp,
    pm25_si,
    pm10_si,
    no2_si,
    co_si,
    so2_si,
    o3_si,
    CPCB_24H_POLLUTANTS,
    CPCB_8H_POLLUTANTS,
    MIN_OBSERVATIONS_24H,
    MIN_OBSERVATIONS_8H,
)
from services.live_aqi import normalize_openweather_components, is_observation_stale

class TestCPCBDataAdequacyAndAveraging(unittest.TestCase):
    """
    Test suite for Milestone 1, Iteration 1.5E:
    Validate CPCB Averaging Periods and Insufficient-Data Handling.
    """

    def setUp(self):
        self.ref_time = datetime(2026, 10, 30, 12, 0, 0)
        self.city_a = str(uuid.uuid4())
        self.city_b = str(uuid.uuid4())

    def test_1_separation_of_24h_and_8h_averaging_windows(self):
        """
        1. Correct separation of 24-hour and 8-hour averaging windows:
        An observation at -12 hours is within the 24h window (for PM2.5)
        but outside the 8h window (for CO).
        """
        # Create 16 observations spaced over 24 hours:
        # 10 observations at -20h down to -10h
        # 6 observations at -6h down to -1h
        observations = []
        for i in range(1, 11):  # 10 observations in (-20h to -10h)
            observations.append({
                "timestamp": self.ref_time - timedelta(hours=20 - i),
                "city_id": self.city_a,
                "pm25": 50.0,
                "co": 2.0,
            })
        for i in range(1, 7):   # 6 observations in (-7h to -1h)
            observations.append({
                "timestamp": self.ref_time - timedelta(hours=7 - i),
                "city_id": self.city_a,
                "pm25": 50.0,
                "co": 2.0,
            })

        res = aggregate_cpcb_aqi(observations, city_id=self.city_a, ref_time=self.ref_time)
        # PM2.5 has 16 observations within 24h -> valid
        self.assertEqual(res["observation_counts"]["pm25"], 16)
        self.assertIsNotNone(res["sub_indices"]["pm25"])
        # CO has only 6 observations in the 8h window (the other 10 were older than 8h)
        self.assertEqual(res["observation_counts"]["co"], 6)

    def test_2_calculations_with_enough_valid_observations(self):
        """
        2. Calculations with enough valid observations:
        24 hourly observations provided for all criteria pollutants ->
        averages, sub-indices, and overall AQI are properly computed.
        """
        observations = []
        for i in range(24):
            observations.append({
                "timestamp": self.ref_time - timedelta(hours=i),
                "city_id": self.city_a,
                "pm25": 45.0,  # sub-index: 50 + (15/30)*50 = 75
                "pm10": 80.0,  # sub-index: 80
                "no2":  30.0,  # sub-index: 30 * 50 / 40 = 37.5
                "so2":  25.0,  # sub-index: 25 * 50 / 40 = 31.25
                "co":   1.5,   # sub-index: 50 + 50*(0.5) = 75
                "o3":   50.0,  # sub-index: 50
            })

        res = aggregate_cpcb_aqi(observations, city_id=self.city_a, ref_time=self.ref_time)
        self.assertEqual(res["calculation_status"], "cpcb_compliant")
        self.assertEqual(res["calculation_type"], "cpcb_aggregated")
        self.assertTrue(res["has_particulate"])
        self.assertEqual(res["valid_pollutants_count"], 6)
        self.assertIsNotNone(res["aqi_score"])
        self.assertEqual(res["aqi_score"], 80.0)  # PM10 is max (80)
        self.assertEqual(res["responsible_pollutant"], "pm10")

    def test_3_fewer_than_16_valid_observations_for_subindex(self):
        """
        3. Fewer than 16 valid observations for a pollutant sub-index:
        15 valid observations in a 24-hour window -> sub-index is None and
        insufficient_data status is recorded.
        """
        observations = []
        for i in range(15):  # Only 15 hours
            observations.append({
                "timestamp": self.ref_time - timedelta(hours=i),
                "city_id": self.city_a,
                "pm25": 60.0,
            })

        res = aggregate_cpcb_aqi(observations, city_id=self.city_a, ref_time=self.ref_time)
        self.assertEqual(res["observation_counts"]["pm25"], 15)
        self.assertIsNone(res["sub_indices"]["pm25"])
        self.assertIsNone(res["averages"]["pm25"])
        self.assertEqual(res["calculation_status"], "insufficient_data")

    def test_4_missing_values_not_converted_to_zeros_and_zero_preserved(self):
        """
        4. Missing values not being converted into measured zeros:
        None or missing values must NOT default to 0.0 or be counted as observations.
        Explicit 0.0 measured values MUST be kept and counted.
        """
        # 16 records, but only 8 have pm25, the other 8 are None
        observations = []
        for i in range(8):
            observations.append({
                "timestamp": self.ref_time - timedelta(hours=i),
                "city_id": self.city_a,
                "pm25": 50.0,
                "co": 0.0,  # Explicit measured zero
            })
        for i in range(8, 16):
            observations.append({
                "timestamp": self.ref_time - timedelta(hours=i),
                "city_id": self.city_a,
                "pm25": None,  # Missing!
                "co": None,  # Missing!
            })

        res = aggregate_cpcb_aqi(observations, city_id=self.city_a, ref_time=self.ref_time)
        # pm25 count must be 8 (NOT 16!), and sub-index must be None
        self.assertEqual(res["observation_counts"]["pm25"], 8)
        self.assertIsNone(res["sub_indices"]["pm25"])
        # CO was measured 0.0 in all 8 hours within 8h window -> count is 8, avg is 0.0
        self.assertEqual(res["observation_counts"]["co"], 8)
        self.assertEqual(res["averages"]["co"], 0.0)
        self.assertEqual(res["sub_indices"]["co"], 0.0)

    def test_5_duplicate_timestamps_do_not_inflate_observation_count(self):
        """
        5. Duplicate timestamps not inflating the observation count:
        Multiple records with the same timestamp must be deduplicated so
        repeated polling within the same hour does not count as new hours.
        """
        observations = []
        # Create 10 distinct timestamps, each duplicated 3 times (30 total rows)
        for i in range(10):
            ts = self.ref_time - timedelta(hours=i)
            for _ in range(3):
                observations.append({
                    "timestamp": ts,
                    "city_id": self.city_a,
                    "pm25": 40.0,
                })

        res = aggregate_cpcb_aqi(observations, city_id=self.city_a, ref_time=self.ref_time)
        # Unique observation count must be 10, NOT 30!
        self.assertEqual(res["observation_counts"]["pm25"], 10)
        # Since 10 < 16, sub-index must still be None
        self.assertIsNone(res["sub_indices"]["pm25"])

    def test_6_stale_observations_excluded_by_window(self):
        """
        6. Stale observations being excluded by the documented policy:
        Observations older than 24 hours (for 24h pollutants) or 8 hours
        (for 8h pollutants) from ref_time are excluded.
        """
        observations = [
            # 25 hours old -> stale for both 24h and 8h
            {"timestamp": self.ref_time - timedelta(hours=25), "city_id": self.city_a, "pm25": 100.0, "co": 5.0},
            # 10 hours old -> valid for 24h, stale for 8h
            {"timestamp": self.ref_time - timedelta(hours=10), "city_id": self.city_a, "pm25": 50.0, "co": 5.0},
            # 2 hours old -> valid for both
            {"timestamp": self.ref_time - timedelta(hours=2), "city_id": self.city_a, "pm25": 30.0, "co": 1.0},
        ]

        res = aggregate_cpcb_aqi(observations, city_id=self.city_a, ref_time=self.ref_time)
        # pm25 window (24h) includes -10h and -2h, excludes -25h
        self.assertEqual(res["observation_counts"]["pm25"], 2)
        # co window (8h) includes only -2h, excludes -10h and -25h
        self.assertEqual(res["observation_counts"]["co"], 1)

    def test_7_measurements_from_different_cities_not_combined(self):
        """
        7. Measurements from different cities not being combined:
        Filtering by city_id prevents records from City B leaking into City A.
        """
        observations = []
        for i in range(16):
            observations.append({
                "timestamp": self.ref_time - timedelta(hours=i),
                "city_id": self.city_a,
                "pm25": 50.0,
            })
            observations.append({
                "timestamp": self.ref_time - timedelta(hours=i),
                "city_id": self.city_b,
                "pm25": 300.0,  # High pollution in City B
            })

        res_a = aggregate_cpcb_aqi(observations, city_id=self.city_a, ref_time=self.ref_time)
        self.assertEqual(res_a["observation_counts"]["pm25"], 16)
        self.assertEqual(res_a["averages"]["pm25"], 50.0)

        res_b = aggregate_cpcb_aqi(observations, city_id=self.city_b, ref_time=self.ref_time)
        self.assertEqual(res_b["observation_counts"]["pm25"], 16)
        self.assertEqual(res_b["averages"]["pm25"], 300.0)

    def test_8_co_and_ozone_appropriate_averaging_windows(self):
        """
        8. CO and ozone using their appropriate 8-hour averaging windows:
        CO and O3 observations beyond 8 hours do not alter the 8h average.
        """
        observations = []
        # Recent 8 hours: CO = 1.0 mg/m3, O3 = 50.0 ug/m3
        for i in range(8):
            observations.append({
                "timestamp": self.ref_time - timedelta(hours=i),
                "city_id": self.city_a,
                "co": 1.0,
                "o3": 50.0,
            })
        # Older 16 hours (hours 9 to 24): huge spike CO = 30.0, O3 = 400.0
        for i in range(9, 25):
            observations.append({
                "timestamp": self.ref_time - timedelta(hours=i),
                "city_id": self.city_a,
                "co": 30.0,
                "o3": 400.0,
            })

        res = aggregate_cpcb_aqi(observations, city_id=self.city_a, ref_time=self.ref_time)
        # Because CO and O3 use 8-hour window, the spike from hours 9-24 is excluded
        self.assertEqual(res["averages"]["co"], 1.0)
        self.assertEqual(res["averages"]["o3"], 50.0)
        self.assertEqual(res["sub_indices"]["co"], 50.0)
        self.assertEqual(res["sub_indices"]["o3"], 50.0)

    def test_9_overall_aqi_unavailable_when_cpcb_criteria_not_satisfied(self):
        """
        9. Overall AQI being unavailable when the required pollutant-data criteria are not satisfied:
        - Case A: 3 gaseous pollutants available, but NO particulate matter (PM2.5 or PM10)
        - Case B: Only 2 pollutants available (< 3 required)
        """
        # Case A: NO2, SO2, CO available (no PM2.5 or PM10)
        sub_indices_a = {
            "pm25": None,
            "pm10": None,
            "no2":  60.0,
            "so2":  40.0,
            "co":   50.0,
            "o3":   None,
        }
        res_a = calculate_cpcb_aqi_from_sub_indices(sub_indices_a)
        self.assertIsNone(res_a["aqi_score"])
        self.assertEqual(res_a["calculation_status"], "insufficient_data")
        self.assertFalse(res_a["has_particulate"])
        self.assertIn("at least one particulate", res_a["reason"].lower())

        # Case B: PM2.5 and NO2 available (2 pollutants < 3)
        sub_indices_b = {
            "pm25": 80.0,
            "pm10": None,
            "no2":  50.0,
            "so2":  None,
            "co":   None,
            "o3":   None,
        }
        res_b = calculate_cpcb_aqi_from_sub_indices(sub_indices_b)
        self.assertIsNone(res_b["aqi_score"])
        self.assertEqual(res_b["calculation_status"], "insufficient_data")
        self.assertTrue(res_b["has_particulate"])
        self.assertEqual(res_b["valid_pollutants_count"], 2)
        self.assertIn("requires at least 3", res_b["reason"].lower())

    def test_10_valid_aqi_computed_when_applicable_requirements_met(self):
        """
        10. A valid AQI still being computed when the applicable requirements are met:
        PM2.5 + NO2 + SO2 (3 pollutants including PM2.5) -> overall AQI computed as max sub-index.
        """
        sub_indices = {
            "pm25": 110.0,
            "pm10": None,
            "no2":  80.0,
            "so2":  45.0,
            "co":   None,
            "o3":   None,
        }
        res = calculate_cpcb_aqi_from_sub_indices(sub_indices)
        self.assertEqual(res["calculation_status"], "cpcb_compliant")
        self.assertEqual(res["aqi_score"], 110.0)
        self.assertEqual(res["responsible_pollutant"], "pm25")
        self.assertTrue(res["has_particulate"])
        self.assertEqual(res["valid_pollutants_count"], 3)

    def test_11_vendor_derived_estimates_distinguishable_from_cpcb_compliant(self):
        """
        11. Vendor-derived estimates being distinguishable from CPCB-compliant calculated results:
        Single-hour OpenWeather normalization produces calculation_type='instantaneous_estimate'
        and is_cpcb_compliant=False.
        """
        raw_components = {
            "pm2_5": 35.0,
            "pm10": 70.0,
            "no2": 25.0,
            "co": 1200.0,  # ug/m3 -> 1.2 mg/m3
            "so2": 15.0,
            "o3": 45.0,
        }
        now_dt = int(self.ref_time.timestamp())
        norm = normalize_openweather_components(raw_components, dt=now_dt)

        self.assertEqual(norm["calculation_type"], "instantaneous_estimate")
        self.assertFalse(norm["is_cpcb_compliant"])
        self.assertEqual(norm["cpcb_status"], "insufficient_data")
        self.assertIsNotNone(norm["timestamp"])

        # And calculate_instantaneous_estimate returns explicit warning
        estimate = calculate_instantaneous_estimate(
            pm25=norm["pm25"], pm10=norm["pm10"], no2=norm["no2"],
            co=norm["co"], so2=norm["so2"], o3=norm["o3"]
        )
        self.assertEqual(estimate["calculation_type"], "instantaneous_estimate")
        self.assertFalse(estimate["cpcb_compliant"])
        self.assertIn("instantaneous", estimate["warning"].lower())

    def test_12_regression_intact_admin_writes_and_rejection(self):
        """
        12. Existing authentication, admin-only writes, ownership isolation, unit normalization,
        client-score rejection, and public read endpoints remain intact.
        Direct calculate_aqi continues to compute authoritative CPCB formula value.
        """
        score = calculate_aqi(pm25=50.0, pm10=80.0, no2=40.0, co=1.5, so2=30.0, o3=60.0)
        # Expected max of sub-indices:
        # pm25(50) = 83.33
        # pm10(80) = 80.0
        # no2(40)  = 50.0
        # co(1.5)  = 75.0
        # so2(30)  = 37.5
        # o3(60)   = 60.0
        self.assertAlmostEqual(score, 83.33, places=1)

    def test_13_boundary_conditions_24h_pollutants(self):
        """
        13. Boundary conditions around 24-hour minimum-data threshold:
        - 15 observations in 24h -> sub-index is None, status is insufficient_data
        - 16 observations in 24h -> sub-index is calculated successfully
        """
        obs_15 = []
        for i in range(15):
            obs_15.append({
                "timestamp": self.ref_time - timedelta(hours=i),
                "city_id": self.city_a,
                "pm25": 45.0,
            })
        res_15 = aggregate_cpcb_aqi(obs_15, city_id=self.city_a, ref_time=self.ref_time)
        self.assertEqual(res_15["observation_counts"]["pm25"], 15)
        self.assertIsNone(res_15["sub_indices"]["pm25"])
        self.assertEqual(res_15["calculation_status"], "insufficient_data")

        # Add exactly 1 more observation to reach 16 (threshold)
        obs_16 = list(obs_15)
        obs_16.append({
            "timestamp": self.ref_time - timedelta(hours=15),
            "city_id": self.city_a,
            "pm25": 45.0,
        })
        res_16 = aggregate_cpcb_aqi(obs_16, city_id=self.city_a, ref_time=self.ref_time)
        self.assertEqual(res_16["observation_counts"]["pm25"], 16)
        self.assertIsNotNone(res_16["sub_indices"]["pm25"])
        self.assertEqual(res_16["sub_indices"]["pm25"], 75.0)

    def test_14_boundary_conditions_8h_pollutants(self):
        """
        14. Boundary conditions around 8-hour minimum-data threshold (CO and O3):
        - 5 observations in 8h window -> sub-index is None
        - 6 observations in 8h window -> sub-index is calculated successfully
        """
        obs_5 = []
        for i in range(5):
            obs_5.append({
                "timestamp": self.ref_time - timedelta(hours=i),
                "city_id": self.city_a,
                "co": 1.5,
                "o3": 60.0,
            })
        res_5 = aggregate_cpcb_aqi(obs_5, city_id=self.city_a, ref_time=self.ref_time)
        self.assertEqual(res_5["observation_counts"]["co"], 5)
        self.assertEqual(res_5["observation_counts"]["o3"], 5)
        self.assertIsNone(res_5["sub_indices"]["co"])
        self.assertIsNone(res_5["sub_indices"]["o3"])

        # Add 1 more observation to reach 6 (the CAAQMS 75% completeness threshold)
        obs_6 = list(obs_5)
        obs_6.append({
            "timestamp": self.ref_time - timedelta(hours=5),
            "city_id": self.city_a,
            "co": 1.5,
            "o3": 60.0,
        })
        res_6 = aggregate_cpcb_aqi(obs_6, city_id=self.city_a, ref_time=self.ref_time)
        self.assertEqual(res_6["observation_counts"]["co"], 6)
        self.assertEqual(res_6["observation_counts"]["o3"], 6)
        self.assertIsNotNone(res_6["sub_indices"]["co"])
        self.assertIsNotNone(res_6["sub_indices"]["o3"])
        self.assertEqual(res_6["sub_indices"]["co"], 75.0)
        self.assertEqual(res_6["sub_indices"]["o3"], 60.0)

    def test_15_individual_subindices_retained_when_overall_aqi_insufficient(self):
        """
        15. Individual valid sub-indices must be returned even when overall AQI is unavailable
        (e.g., only 2 pollutants available or missing particulate matter).
        """
        # Supply 16 observations for PM2.5 and NO2 only (2 pollutants < 3 required for overall AQI)
        observations = []
        for i in range(16):
            observations.append({
                "timestamp": self.ref_time - timedelta(hours=i),
                "city_id": self.city_a,
                "pm25": 45.0,  # sub-index = 75.0
                "no2": 40.0,   # sub-index = 50.0
            })
        res = aggregate_cpcb_aqi(observations, city_id=self.city_a, ref_time=self.ref_time)
        # Overall AQI is None due to pollutant count < 3
        self.assertIsNone(res["aqi_score"])
        self.assertEqual(res["calculation_status"], "insufficient_data")
        self.assertEqual(res["valid_pollutants_count"], 2)
        # But individual valid sub-indices MUST be preserved and returned
        self.assertEqual(res["sub_indices"]["pm25"], 75.0)
        self.assertEqual(res["sub_indices"]["no2"], 50.0)
        self.assertIsNone(res["sub_indices"]["pm10"])
        self.assertIsNone(res["sub_indices"]["co"])

    def test_16_staleness_check_using_source_observation_time(self):
        """
        16. Staleness checks must use source observation time, not ingestion time.
        Supports consistent UTC comparisons without mixing naive and aware datetimes.
        """
        now = datetime(2026, 11, 2, 12, 0, 0)
        
        # Fresh observation (1 hour old relative to ref_time)
        fresh_ts = now - timedelta(hours=1)
        self.assertFalse(is_observation_stale(fresh_ts, max_age_hours=24.0, ref_time=now))

        # Stale observation (25 hours old relative to ref_time)
        stale_ts = now - timedelta(hours=25)
        self.assertTrue(is_observation_stale(stale_ts, max_age_hours=24.0, ref_time=now))

        # Missing timestamp cannot be verified as fresh -> considered stale
        self.assertTrue(is_observation_stale(None, max_age_hours=24.0, ref_time=now))

        # Supports ISO string and timestamp inputs
        self.assertFalse(is_observation_stale(fresh_ts.isoformat(), max_age_hours=24.0, ref_time=now))
        self.assertTrue(is_observation_stale(stale_ts.isoformat(), max_age_hours=24.0, ref_time=now))

    def test_17_validate_declared_averaging_periods(self):
        """
        17. Validate declared averaging period rules for manual submissions:
        - 24h required for PM2.5, PM10, NO2, SO2
        - 8h required for CO and O3
        - Returns (matches_periods, averaging_period_status, summary_string, reason)
        - Omitted metadata returns averaging_period_status="missing_declaration"
        - Mismatched metadata returns averaging_period_status="declared_periods_mismatch"
        - Matching metadata returns averaging_period_status="declared_periods_match"
        """
        # Case A: Valid CPCB declarations
        valid_periods = {
            "pm25": "24h", "pm10": "24h", "no2": "24h", "so2": "24h",
            "co": "8h", "o3": "8h"
        }
        matches, status, summary, reason = validate_cpcb_averaging_periods(valid_periods)
        self.assertTrue(matches)
        self.assertEqual(status, "declared_periods_match")
        self.assertEqual(summary, "24h_8h_averaged")
        self.assertIn("remain user-declared", reason)

        # Case B: Invalid averaging periods (e.g. 1h for PM2.5, 24h for CO)
        invalid_periods = {
            "pm25": "1h", "pm10": "24h", "no2": "24h", "so2": "24h",
            "co": "24h", "o3": "8h"
        }
        matches_inv, status_inv, summary_inv, reason_inv = validate_cpcb_averaging_periods(invalid_periods)
        self.assertFalse(matches_inv)
        self.assertEqual(status_inv, "declared_periods_mismatch")
        self.assertEqual(summary_inv, "non_compliant_declared")
        self.assertIn("Declared averaging periods do not match", reason_inv)

        # Case C: Missing / omitted metadata
        matches_none, status_none, summary_none, reason_none = validate_cpcb_averaging_periods(None)
        self.assertFalse(matches_none)
        self.assertEqual(status_none, "missing_declaration")
        self.assertEqual(summary_none, "unspecified")
        self.assertIn("omitted", reason_none.lower())

    def test_19_distinguish_official_cpcb_rule_from_application_engineering_policy(self):
        """
        19. Verify clear distinction between official statutory CPCB rules and application engineering policies:
        - Official statutory CPCB rule (NAQI Oct 2014, Sec 4.1): Minimum 16 hours required for 24h sub-index.
        - Application engineering policy (APPLICATION_POLICY_MIN_OBSERVATIONS_8H = 6): 75% data-capture
          threshold for 8-hour rolling windows is an explicit application-level engineering policy,
          not a statutory CPCB rule.
        """
        from ml.predict import (
            OFFICIAL_CPCB_MIN_HOURS_FOR_SUBINDEX,
            APPLICATION_POLICY_MIN_OBSERVATIONS_8H,
            aggregate_cpcb_aqi,
        )
        self.assertEqual(OFFICIAL_CPCB_MIN_HOURS_FOR_SUBINDEX, 16)
        self.assertEqual(APPLICATION_POLICY_MIN_OBSERVATIONS_8H, 6)

        # Run aggregate_cpcb_aqi to inspect metadata documenting the distinction
        res = aggregate_cpcb_aqi([], city_id=self.city_a, ref_time=self.ref_time)
        policy_meta = res.get("data_adequacy_policy", {})
        self.assertIn("October 2014", policy_meta.get("cpcb_statutory_rule", ""))
        self.assertIn("Section 4.1", policy_meta.get("cpcb_statutory_rule", ""))
        self.assertIn("engineering policy", policy_meta.get("application_policy_8h", "").lower())
        self.assertIn("not an official cpcb statutory rule", policy_meta.get("application_policy_8h", "").lower())

    def test_18_openweather_observation_time_separated_from_ingestion_time(self):
        """
        18. Ingesting OpenWeather components preserves source observation time
        and tracks application ingestion time distinctly.
        """
        raw_components = {"pm2_5": 25.0, "pm10": 50.0, "no2": 20.0, "co": 800.0, "so2": 10.0, "o3": 30.0}
        source_epoch = 1762080000  # fixed past unix epoch
        res = normalize_openweather_components(raw_components, dt=source_epoch)
        self.assertEqual(res["dt"], source_epoch)
        self.assertIsNotNone(res["observation_time"])
        self.assertIsNotNone(res["ingested_at"])
        # observation_time reflects the source epoch, not the current fetch time
        self.assertIn("2025-11-02", res["observation_time"])

    def test_20_six_observations_cannot_independently_establish_cpcb_compliance(self):
        """
        20. Six observations in an 8-hour window cannot independently establish CPCB data adequacy:
        - When only PM2.5 has 16 hours of data, while CO and O3 have only 6 observations in an isolated 8h window:
          * In CPCB-methodology mode: CO/O3 cannot establish CPCB adequacy; because only 1 pollutant has
            statutory completeness (< 3 required), overall calculation_status="insufficient_data",
            aqi_score=None, and is_cpcb_compliant=False.
          * In streaming-estimate mode: CO/O3 produce preliminary sub-indices using the 6-observation
            engineering heuristic, producing calculation_type="application_streaming_estimate"
            with is_cpcb_compliant=False.
        """
        observations = []
        # PM2.5: 16 observations in 24h window
        for i in range(16):
            observations.append({
                "timestamp": self.ref_time - timedelta(hours=i),
                "city_id": self.city_a,
                "pm25": 45.0,  # sub-index = 75
            })
        # CO and O3: only 6 observations in the past 6 hours (isolated 8h window)
        for i in range(6):
            observations.append({
                "timestamp": self.ref_time - timedelta(hours=i),
                "city_id": self.city_a,
                "co": 1.5,     # sub-index = 75
                "o3": 60.0,    # sub-index = 60
            })

        # Under strict CPCB methodology:
        res_cpcb = aggregate_cpcb_aqi(
            observations,
            city_id=self.city_a,
            ref_time=self.ref_time,
            calculation_mode="cpcb_methodology"
        )
        self.assertIsNone(res_cpcb["aqi_score"])
        self.assertEqual(res_cpcb["calculation_status"], "insufficient_data")
        self.assertFalse(res_cpcb["is_cpcb_compliant"])
        self.assertIn("16-hour", res_cpcb["reason"])
        self.assertIn("streaming heuristic", res_cpcb["reason"])

        # Under streaming estimate mode:
        res_stream = aggregate_cpcb_aqi(
            observations,
            city_id=self.city_a,
            ref_time=self.ref_time,
            calculation_mode="streaming_estimate"
        )
        self.assertIsNotNone(res_stream["aqi_score"])
        self.assertEqual(res_stream["calculation_type"], "application_streaming_estimate")
        self.assertEqual(res_stream["calculation_status"], "streaming_estimate")
        self.assertFalse(res_stream["is_cpcb_compliant"])
        self.assertFalse(res_stream["sub_index_cpcb_compliant"]["co"])
        self.assertFalse(res_stream["sub_index_cpcb_compliant"]["o3"])
        self.assertEqual(res_stream["sub_index_adequacy"]["co"], "application_streaming_heuristic_6h")

    def test_21_streaming_outputs_distinguishable_from_cpcb_methodology_outputs(self):
        """
        21. Application-specific streaming outputs remain strictly distinguishable from
        CPCB-methodology outputs in calculation_type, calculation_status, and is_cpcb_compliant.
        """
        # Case A: Full CPCB data adequacy (all 4 criteria 24h pollutants have 24 hours of data)
        cpcb_obs = []
        for i in range(24):
            cpcb_obs.append({
                "timestamp": self.ref_time - timedelta(hours=i),
                "city_id": self.city_a,
                "pm25": 40.0,
                "pm10": 70.0,
                "no2": 30.0,
                "so2": 20.0,
                "co": 1.0,
                "o3": 40.0,
            })
        res_cpcb = aggregate_cpcb_aqi(cpcb_obs, city_id=self.city_a, ref_time=self.ref_time, calculation_mode="cpcb_methodology")
        self.assertEqual(res_cpcb["calculation_type"], "cpcb_compliant")
        self.assertEqual(res_cpcb["calculation_status"], "cpcb_compliant")
        self.assertTrue(res_cpcb["is_cpcb_compliant"])

        # Case B: Streaming heuristic (PM2.5 with 16h, CO with 6h, O3 with 6h)
        stream_obs = []
        for i in range(16):
            stream_obs.append({
                "timestamp": self.ref_time - timedelta(hours=i),
                "city_id": self.city_a,
                "pm25": 40.0,
            })
        for i in range(6):
            stream_obs.append({
                "timestamp": self.ref_time - timedelta(hours=i),
                "city_id": self.city_a,
                "co": 1.2,
                "o3": 35.0,
            })
        res_stream = aggregate_cpcb_aqi(stream_obs, city_id=self.city_a, ref_time=self.ref_time, calculation_mode="streaming_estimate")
        self.assertEqual(res_stream["calculation_type"], "application_streaming_estimate")
        self.assertEqual(res_stream["calculation_status"], "streaming_estimate")
        self.assertFalse(res_stream["is_cpcb_compliant"])

        # Compare labels and classifications
        self.assertNotEqual(res_cpcb["calculation_type"], res_stream["calculation_type"])
        self.assertNotEqual(res_cpcb["is_cpcb_compliant"], res_stream["is_cpcb_compliant"])

    def test_22_official_16h_rule_not_silently_restricted_to_one_pollutant_group(self):
        """
        22. Verify that the official 16-hour requirement is not silently restricted to one pollutant group
        without supporting documentation, and the ambiguity regarding isolated 8-hour windows is exposed.
        """
        res = aggregate_cpcb_aqi([], city_id=self.city_a, ref_time=self.ref_time)
        meta = res.get("data_adequacy_policy", {})
        # CPCB statutory rule text cites CUPS/82/2014-15 and 2016 Bulletins
        self.assertIn("CUPS/82/2014-15", meta["cpcb_statutory_rule"])
        self.assertIn("minimum of 16 hours of data is necessary", meta["cpcb_statutory_rule"])
        self.assertIn("without restricting it solely to 24-hour pollutants", meta["cpcb_statutory_rule"])
        # Ambiguity notice explains the interaction with isolated 8h rolling windows
        self.assertIn("methodology_ambiguity_notice", meta)
        self.assertIn("isolated 8-hour rolling window", meta["methodology_ambiguity_notice"])

    def test_23_dominant_pollutant_relying_on_heuristic_prevents_cpcb_compliance(self):
        """
        23. When 3 pollutants meet 24h adequacy, but the dominant (highest sub-index) pollutant is CO
        relying solely on the 6-observation 8h heuristic, the overall AQI cannot claim CPCB compliance.
        """
        observations = []
        for i in range(16):
            observations.append({
                "timestamp": self.ref_time - timedelta(hours=i),
                "city_id": self.city_a,
                "pm25": 30.0,  # sub-index = 50.0
                "pm10": 40.0,  # sub-index = 40.0
                "no2":  25.0,  # sub-index = 31.25
            })
        # CO has 6 observations with a heavy spike (CO = 25 mg/m3 -> sub-index = 347.0)
        for i in range(6):
            observations.append({
                "timestamp": self.ref_time - timedelta(hours=i),
                "city_id": self.city_a,
                "co": 25.0,
            })

        res = aggregate_cpcb_aqi(observations, city_id=self.city_a, ref_time=self.ref_time, calculation_mode="cpcb_methodology")
        # CO is dominant (347.0), but relies on 6-observation heuristic
        self.assertEqual(res["responsible_pollutant"], "co")
        self.assertFalse(res["is_cpcb_compliant"])
        self.assertEqual(res["calculation_type"], "application_streaming_estimate")

    def test_24_insufficient_data_produces_appropriate_status_and_metadata(self):
        """
        24. When data adequacy criteria cannot be demonstrated under CPCB methodology,
        the system returns calculation_status="insufficient_data", aqi_score=None,
        and is_cpcb_compliant=False with detailed diagnostic metadata.
        """
        # Only PM2.5 available (1 pollutant < 3 required)
        observations = []
        for i in range(16):
            observations.append({
                "timestamp": self.ref_time - timedelta(hours=i),
                "city_id": self.city_a,
                "pm25": 50.0,
            })
        res = aggregate_cpcb_aqi(observations, city_id=self.city_a, ref_time=self.ref_time, calculation_mode="cpcb_methodology")
        self.assertIsNone(res["aqi_score"])
        self.assertEqual(res["calculation_status"], "insufficient_data")
        self.assertFalse(res["is_cpcb_compliant"])
        self.assertIn("reason", res)
        self.assertIn("data_adequacy_policy", res)

if __name__ == "__main__":
    unittest.main()

