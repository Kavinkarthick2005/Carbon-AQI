"""
Unit and integration regression tests for shared AQI data ingestion.
Verifies that POST /api/aqi/record:
1. Rejects unauthenticated requests with 401 Unauthorized.
2. Rejects authenticated non-admin users with 403 Forbidden.
3. Rejects authenticated users not present in the database with 404 Not Found.
4. Allows authenticated administrators to ingest AQI readings with 200 OK.
5. Server-side CPCB AQI calculation is strictly authoritative: client-supplied aqi_score
   is ignored in favor of the server-calculated result (both stored and returned).
6. Missing CO and SO2 (or any other of the 6 CPCB criteria pollutants) are rejected with 422.
7. Distinguishes missing measurements from explicit measured zeros (explicit 0.0 is accepted).
8. Rejects null, negative, NaN, and infinity pollutant values with 422.
9. Validates city_id format (UUID) and city existence in the database (400 for bad format, 404 for unknown city).
10. CO2 ppm is tracked separately and is never used as a substitute for CO.
11. Persists administrator provenance in user_id.
12. Passes correct pollutant mapping (pm25, pm10, no2, co, so2, o3) to ML and RL modules.
13. Failed authorization, validation, or city checks create no persisted AQI record.
14. Preserves public unauthenticated read access to public city AQI endpoints.
"""

import os
import sys
import unittest
import uuid
import math
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

os.environ.setdefault("DATABASE_URL", "postgresql://test_user:test_pass@localhost:5432/test_db")

with (
    patch("sqlalchemy.sql.schema.MetaData.create_all"),
    patch.dict(os.environ, {"FIREBASE_CREDENTIALS_PATH": ""}, clear=False),
):
    import firebase_admin
    from firebase_admin import auth
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    from pydantic import ValidationError
    from database import get_db
    from models import User, City, AirQualityData, MLPrediction, MitigationStrategy, AQIHistory
    from schemas import AQIRecord, AQIResponse
    from routers import aqi as aqi_router
    from routers.auth import verify_firebase_token, require_admin_user
    from ml.predict import calculate_aqi, predict_aqi_risk
    from services.live_aqi import normalize_openweather_components


class AQIInMemoryDB:
    """In-memory database session simulation for AQI ingestion and query tests."""

    def __init__(self, users=None, cities=None, aqi_records=None, ml_predictions=None, mitigations=None):
        self.users = list(users or [])
        self.cities = list(cities or [])
        self.aqi_records = list(aqi_records or [])
        self.ml_predictions = list(ml_predictions or [])
        self.mitigations = list(mitigations or [])
        self.added = []
        self.committed = False
        self.rolled_back = False

    def query(self, *entities):
        session = self

        class SubqueryWrapper:
            def __init__(self, c=None):
                class Columns:
                    city_id = "city_id"
                    latest_date = "latest_date"
                self.c = Columns()

        class QueryChain:
            def __init__(self, entities):
                self.entities = entities
                self._filters = []
                self._joins = []
                self._limit = None
                self._order_by = None
                self._group_by = None

            def outerjoin(self, target, onclause=None):
                self._joins.append((target, onclause))
                return self

            def join(self, target, onclause=None, isouter=False):
                self._joins.append((target, onclause))
                return self

            def filter(self, *conditions):
                self._filters.extend(conditions)
                return self

            def group_by(self, *clauses):
                self._group_by = clauses
                return self

            def subquery(self):
                return SubqueryWrapper()

            def order_by(self, *clauses):
                self._order_by = clauses
                return self

            def limit(self, n):
                self._limit = n
                return self

            def _get_matching_items(self):
                if not self.entities:
                    return []
                target_cls = self.entities[0]

                if target_cls == User:
                    pool = session.users + [x for x in session.added if isinstance(x, User)]
                elif target_cls == City:
                    pool = session.cities + [x for x in session.added if isinstance(x, City)]
                elif target_cls == AirQualityData:
                    pool = session.aqi_records + [x for x in session.added if isinstance(x, AirQualityData)]
                elif target_cls == MLPrediction:
                    pool = session.ml_predictions + [x for x in session.added if isinstance(x, MLPrediction)]
                elif target_cls == MitigationStrategy:
                    pool = session.mitigations + [x for x in session.added if isinstance(x, MitigationStrategy)]
                else:
                    pool = []

                results = pool
                for cond in self._filters:
                    left = getattr(cond, "left", None)
                    right = getattr(cond, "right", None)
                    key = getattr(left, "key", None) or getattr(left, "name", None)
                    val = getattr(right, "value", None)
                    if key is not None:
                        results = [item for item in results if getattr(item, key, None) == val]
                return results

            def first(self):
                if len(self.entities) == 2 and self.entities[0] == AirQualityData and self.entities[1] == MLPrediction:
                    all_records = self._get_matching_records()
                    if not all_records:
                        return None
                    record = all_records[0]
                    ml = next(
                        (p for p in (session.ml_predictions + [x for x in session.added if isinstance(x, MLPrediction)])
                         if p.record_id == record.record_id),
                        None
                    )
                    return (record, ml)

                items = self._get_matching_items()
                return items[0] if items else None

            def all(self):
                if len(self.entities) > 2:
                    rows = []
                    all_cities = session.cities + [x for x in session.added if isinstance(x, City)]
                    all_records = session.aqi_records + [x for x in session.added if isinstance(x, AirQualityData)]
                    all_preds = session.ml_predictions + [x for x in session.added if isinstance(x, MLPrediction)]
                    for city in all_cities:
                        city_records = [r for r in all_records if r.city_id == city.city_id]
                        if city_records:
                            latest_r = sorted(city_records, key=lambda r: getattr(r, "recorded_at", datetime.min), reverse=True)[0]
                            pred = next((p for p in all_preds if p.record_id == latest_r.record_id), None)
                            rows.append((
                                city.city_id,
                                city.city_name,
                                latest_r.aqi_score,
                                latest_r.pm25,
                                latest_r.pm10,
                                latest_r.no2,
                                latest_r.o3,
                                pred.risk_level if pred else "Moderate"
                            ))
                    return rows

                if len(self.entities) == 2 and self.entities[0] == AirQualityData and self.entities[1] == MLPrediction:
                    all_records = self._get_matching_records()
                    if self._limit is not None:
                        all_records = all_records[:self._limit]
                    all_preds = session.ml_predictions + [x for x in session.added if isinstance(x, MLPrediction)]
                    pairs = []
                    for r in all_records:
                        pred = next((p for p in all_preds if p.record_id == r.record_id), None)
                        pairs.append((r, pred))
                    return pairs

                items = self._get_matching_items()
                if self._limit is not None:
                    items = items[:self._limit]
                return items

            def _get_matching_records(self):
                pool = session.aqi_records + [x for x in session.added if isinstance(x, AirQualityData)]
                results = pool
                for cond in self._filters:
                    left = getattr(cond, "left", None)
                    right = getattr(cond, "right", None)
                    key = getattr(left, "key", None) or getattr(left, "name", None)
                    val = getattr(right, "value", None)
                    if key is not None:
                        results = [item for item in results if getattr(item, key, None) == val]
                results = sorted(results, key=lambda r: getattr(r, "recorded_at", datetime.min), reverse=True)
                return results

        return QueryChain(entities)

    def add(self, instance):
        if isinstance(instance, AirQualityData):
            if not getattr(instance, "record_id", None):
                instance.record_id = uuid.uuid4()
            if not getattr(instance, "recorded_at", None):
                instance.recorded_at = datetime.now(timezone.utc)
        elif isinstance(instance, MLPrediction):
            if not getattr(instance, "prediction_id", None):
                instance.prediction_id = uuid.uuid4()
            if not getattr(instance, "predicted_at", None):
                instance.predicted_at = datetime.now(timezone.utc)
        elif isinstance(instance, MitigationStrategy):
            if not getattr(instance, "strategy_id", None):
                instance.strategy_id = uuid.uuid4()
            if not getattr(instance, "selected_at", None):
                instance.selected_at = datetime.now(timezone.utc)
        elif isinstance(instance, User) and not getattr(instance, "user_id", None):
            instance.user_id = uuid.uuid4()
        elif isinstance(instance, City) and not getattr(instance, "city_id", None):
            instance.city_id = uuid.uuid4()
        self.added.append(instance)

    def commit(self):
        self.committed = True

    def refresh(self, instance):
        if isinstance(instance, AirQualityData):
            if not getattr(instance, "record_id", None):
                instance.record_id = uuid.uuid4()
            if not getattr(instance, "recorded_at", None):
                instance.recorded_at = datetime.now(timezone.utc)

    def rollback(self):
        self.rolled_back = True


class BaseAQITestCase(unittest.TestCase):
    def setUp(self):
        self.city_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
        self.city = City(
            city_id=self.city_id,
            city_name="Test City",
            country="India",
            latitude=28.6139,
            longitude=77.2090,
            population=1000000
        )
        self.admin_user = User(
            user_id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
            firebase_uid="admin_firebase_uid",
            email="admin@example.com",
            name="Admin User",
            role="admin",
            preferred_language="en"
        )
        self.regular_user = User(
            user_id=uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
            firebase_uid="regular_firebase_uid",
            email="regular@example.com",
            name="Regular User",
            role="user",
            preferred_language="en"
        )
        self.existing_record = AirQualityData(
            record_id=uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc"),
            city_id=self.city_id,
            user_id=self.admin_user.user_id,
            pm25=45.0,
            pm10=80.0,
            co=1.0,
            no2=30.0,
            so2=15.0,
            o3=40.0,
            co2_ppm=410.0,
            aqi_score=95.0,
            recorded_at=datetime.now(timezone.utc)
        )
        self.existing_ml = MLPrediction(
            prediction_id=uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd"),
            record_id=self.existing_record.record_id,
            predicted_aqi=95.0,
            risk_level="Low",
            model_version="xgboost",
            predicted_at=datetime.now(timezone.utc)
        )

        self.db = AQIInMemoryDB(
            users=[self.admin_user, self.regular_user],
            cities=[self.city],
            aqi_records=[self.existing_record],
            ml_predictions=[self.existing_ml]
        )

        self.app = FastAPI()
        self.app.include_router(aqi_router.router, prefix="/api/aqi")
        self.app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(self.app)

    def tearDown(self):
        self.app.dependency_overrides.clear()

    def get_valid_payload(self, **overrides):
        """Helper returning a valid payload containing all 6 required CPCB criteria pollutants."""
        payload = {
            "city_id": str(self.city_id),
            "pm25": 55.0,
            "pm10": 90.0,
            "no2": 35.0,
            "co": 1.2,
            "so2": 15.0,
            "o3": 25.0,
            "co2_ppm": 415.0
        }
        payload.update(overrides)
        return payload


class TestAQIIngestionAuthentication(BaseAQITestCase):
    """Tests verifying that POST /api/aqi/record enforces authentication."""

    def test_record_missing_auth_header_fails_401(self):
        payload = self.get_valid_payload()
        response = self.client.post("/api/aqi/record", json=payload)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Authorization header missing")
        self.assertEqual(len(self.db.added), 0, "No record should be persisted on 401")

    def test_record_invalid_token_fails_401(self):
        payload = self.get_valid_payload()
        with (
            patch.object(firebase_admin, "_apps", {"[DEFAULT]": object()}),
            patch.object(auth, "verify_id_token", side_effect=auth.InvalidIdTokenError("Invalid token")),
        ):
            response = self.client.post(
                "/api/aqi/record",
                json=payload,
                headers={"Authorization": "Bearer bad_token"}
            )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Invalid authentication token")
        self.assertEqual(len(self.db.added), 0, "No record should be persisted on invalid token")


class TestAQIIngestionAuthorization(BaseAQITestCase):
    """Tests verifying that POST /api/aqi/record enforces database administrator role."""

    def test_record_regular_user_fails_403(self):
        self.app.dependency_overrides[verify_firebase_token] = lambda: {"uid": self.regular_user.firebase_uid}
        payload = self.get_valid_payload()
        response = self.client.post(
            "/api/aqi/record",
            json=payload,
            headers={"Authorization": "Bearer regular_token"}
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "Admin access required")
        self.assertEqual(len(self.db.added), 0, "No record should be persisted on 403")

    def test_record_unregistered_user_fails_404(self):
        self.app.dependency_overrides[verify_firebase_token] = lambda: {"uid": "unregistered_uid_123"}
        payload = self.get_valid_payload()
        response = self.client.post(
            "/api/aqi/record",
            json=payload,
            headers={"Authorization": "Bearer unregistered_token"}
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "User not found")
        self.assertEqual(len(self.db.added), 0, "No record should be persisted on 404")

    def test_record_admin_user_allowed_200(self):
        self.app.dependency_overrides[verify_firebase_token] = lambda: {"uid": self.admin_user.firebase_uid}
        payload = self.get_valid_payload()
        response = self.client.post(
            "/api/aqi/record",
            json=payload,
            headers={"Authorization": "Bearer admin_token"}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["city_id"], str(self.city_id))
        self.assertEqual(data["pm25"], 55.0)
        self.assertEqual(data["pm10"], 90.0)
        self.assertEqual(data["co"], 1.2)
        self.assertEqual(data["so2"], 15.0)
        self.assertIn("record_id", data)
        self.assertIn("risk_level", data)


class TestAQIIngestionAuthoritativeCalculation(BaseAQITestCase):
    """Tests proving that server-side AQI calculation is strictly authoritative."""

    def setUp(self):
        super().setUp()
        self.app.dependency_overrides[verify_firebase_token] = lambda: {"uid": self.admin_user.firebase_uid}

    def test_client_supplied_aqi_score_is_ignored_and_authoritative_stored_and_returned(self):
        """Prove that a client submitting a custom aqi_score cannot alter the stored or returned score."""
        # Expected CPCB AQI from formula
        expected_aqi = round(float(calculate_aqi(60.0, 100.0, 40.0, 1.0, 20.0, 50.0)), 2)
        
        # Client submits arbitrary aqi_score = 999.0
        payload = {
            "city_id": str(self.city_id),
            "pm25": 60.0,
            "pm10": 100.0,
            "no2": 40.0,
            "co": 1.0,
            "so2": 20.0,
            "o3": 50.0,
            "aqi_score": 999.0
        }
        response = self.client.post(
            "/api/aqi/record",
            json=payload,
            headers={"Authorization": "Bearer admin_token"}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        
        # Returned AQI must be the authoritative server-calculated result, not 999.0
        self.assertEqual(data["aqi_score"], expected_aqi)
        self.assertNotEqual(data["aqi_score"], 999.0)
        
        # Persisted record in database must also be authoritative, not 999.0
        added_aqi = [x for x in self.db.added if isinstance(x, AirQualityData)]
        self.assertEqual(len(added_aqi), 1)
        self.assertEqual(added_aqi[0].aqi_score, expected_aqi)
        self.assertNotEqual(added_aqi[0].aqi_score, 999.0)

    def test_server_calculated_aqi_when_omitted(self):
        """When aqi_score is omitted, server derives and stores the exact CPCB AQI."""
        expected_aqi = round(float(calculate_aqi(60.0, 100.0, 40.0, 1.0, 20.0, 50.0)), 2)
        payload = {
            "city_id": str(self.city_id),
            "pm25": 60.0,
            "pm10": 100.0,
            "no2": 40.0,
            "co": 1.0,
            "so2": 20.0,
            "o3": 50.0
        }
        response = self.client.post(
            "/api/aqi/record",
            json=payload,
            headers={"Authorization": "Bearer admin_token"}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["aqi_score"], expected_aqi)
        added_aqi = [x for x in self.db.added if isinstance(x, AirQualityData)]
        self.assertEqual(added_aqi[0].aqi_score, expected_aqi)

    def test_record_admin_provenance_persisted(self):
        payload = self.get_valid_payload()
        response = self.client.post(
            "/api/aqi/record",
            json=payload,
            headers={"Authorization": "Bearer admin_token"}
        )
        self.assertEqual(response.status_code, 200)
        added_aqi = [x for x in self.db.added if isinstance(x, AirQualityData)]
        self.assertEqual(len(added_aqi), 1)
        record = added_aqi[0]
        self.assertEqual(record.user_id, self.admin_user.user_id)
        self.assertEqual(record.city_id, self.city_id)

    def test_record_parameter_mapping_to_ml_and_rl(self):
        """Verify that predict_aqi_risk and get_mitigation_strategy receive co and so2 correctly."""
        payload = {
            "city_id": str(self.city_id),
            "pm25": 150.0,
            "pm10": 220.0,
            "no2": 95.0,
            "co": 3.5,
            "so2": 45.0,
            "o3": 110.0,
            "co2_ppm": 480.0
        }
        with (
            patch("routers.aqi.predict_aqi_risk") as mock_predict,
            patch("routers.aqi.get_mitigation_strategy") as mock_mitigate
        ):
            mock_predict.return_value = {
                "predicted_aqi": 320.0,
                "risk_level": "High",
                "risk_label": 2,
                "confidence": 0.95
            }
            mock_mitigate.return_value = {
                "strategy_name": "Emergency Health Advisory",
                "description": "Stay indoors",
                "reduction_target": 0.25,
                "estimated_impact": 0.20,
                "action_type": "emergency"
            }
            response = self.client.post(
                "/api/aqi/record",
                json=payload,
                headers={"Authorization": "Bearer admin_token"}
            )
            self.assertEqual(response.status_code, 200)

            # Check positional arguments to predict_aqi_risk: (pm25, pm10, no2, co, so2, o3)
            mock_predict.assert_called_once_with(150.0, 220.0, 95.0, 3.5, 45.0, 110.0)

            # Check positional arguments to get_mitigation_strategy: (pm25, pm10, no2, co, so2, o3)
            mock_mitigate.assert_called_once_with(150.0, 220.0, 95.0, 3.5, 45.0, 110.0)


class TestAQIIngestionMissingAndInvalidData(BaseAQITestCase):
    """Tests proving that missing pollutants are rejected (not defaulted to zero), and valid zeros are accepted."""

    def setUp(self):
        super().setUp()
        self.app.dependency_overrides[verify_firebase_token] = lambda: {"uid": self.admin_user.firebase_uid}

    def test_missing_co_rejected_with_422(self):
        """Missing CO must not default to zero; request must be rejected."""
        payload = self.get_valid_payload()
        del payload["co"]
        response = self.client.post(
            "/api/aqi/record",
            json=payload,
            headers={"Authorization": "Bearer admin_token"}
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(len(self.db.added), 0, "No record should be persisted when co is missing")

    def test_missing_so2_rejected_with_422(self):
        """Missing SO2 must not default to zero; request must be rejected."""
        payload = self.get_valid_payload()
        del payload["so2"]
        response = self.client.post(
            "/api/aqi/record",
            json=payload,
            headers={"Authorization": "Bearer admin_token"}
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(len(self.db.added), 0, "No record should be persisted when so2 is missing")

    def test_all_criteria_pollutants_are_required(self):
        """Each of the 6 criteria pollutants used in calculate_aqi must be present."""
        for pollutant in ["pm25", "pm10", "no2", "co", "so2", "o3"]:
            with self.subTest(missing_pollutant=pollutant):
                payload = self.get_valid_payload()
                del payload[pollutant]
                response = self.client.post(
                    "/api/aqi/record",
                    json=payload,
                    headers={"Authorization": "Bearer admin_token"}
                )
                self.assertEqual(response.status_code, 422)
                self.assertEqual(len(self.db.added), 0)

    def test_explicit_zero_measurements_are_accepted_and_calculated(self):
        """An explicit measured zero (e.g. co: 0.0, so2: 0.0) is accepted and calculated as 0.0 sub-index."""
        payload = self.get_valid_payload(co=0.0, so2=0.0)
        response = self.client.post(
            "/api/aqi/record",
            json=payload,
            headers={"Authorization": "Bearer admin_token"}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["co"], 0.0)
        self.assertEqual(data["so2"], 0.0)
        
        added_aqi = [x for x in self.db.added if isinstance(x, AirQualityData)]
        self.assertEqual(len(added_aqi), 1)
        self.assertEqual(added_aqi[0].co, 0.0)
        self.assertEqual(added_aqi[0].so2, 0.0)

    def test_null_pollutant_values_rejected_with_422(self):
        """Explicit None/null must not be coerced to zero."""
        for pollutant in ["pm25", "pm10", "no2", "co", "so2", "o3"]:
            with self.subTest(null_pollutant=pollutant):
                payload = self.get_valid_payload()
                payload[pollutant] = None
                response = self.client.post(
                    "/api/aqi/record",
                    json=payload,
                    headers={"Authorization": "Bearer admin_token"}
                )
                self.assertEqual(response.status_code, 422)
                self.assertEqual(len(self.db.added), 0)

    def test_negative_pollutant_values_rejected_with_422(self):
        """Negative pollutant values must be rejected."""
        for pollutant in ["pm25", "pm10", "no2", "co", "so2", "o3", "co2_ppm", "aqi_score"]:
            with self.subTest(negative_pollutant=pollutant):
                payload = self.get_valid_payload()
                payload[pollutant] = -1.0
                response = self.client.post(
                    "/api/aqi/record",
                    json=payload,
                    headers={"Authorization": "Bearer admin_token"}
                )
                self.assertEqual(response.status_code, 422)
                self.assertEqual(len(self.db.added), 0)

    def test_non_finite_pollutant_values_rejected(self):
        """NaN and infinity values must be rejected."""
        for field in ["pm25", "pm10", "no2", "co", "so2", "o3", "co2_ppm", "aqi_score"]:
            with self.subTest(field=field, val="nan"):
                data = self.get_valid_payload()
                data[field] = float("nan")
                with self.assertRaises(ValidationError):
                    AQIRecord(**data)

            with self.subTest(field=field, val="inf"):
                data = self.get_valid_payload()
                data[field] = float("inf")
                with self.assertRaises(ValidationError):
                    AQIRecord(**data)

        # Test HTTP endpoint rejects unparseable float values with 422
        payload = self.get_valid_payload(pm25="not-a-number")
        response = self.client.post(
            "/api/aqi/record",
            json=payload,
            headers={"Authorization": "Bearer admin_token"}
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(len(self.db.added), 0)

    def test_co2_ppm_is_tracked_separately_and_never_substitutes_for_co(self):
        """CO2 ppm is distinct from CO. Supplying co2_ppm without co must be rejected with 422."""
        payload = self.get_valid_payload(co2_ppm=430.0)
        del payload["co"]
        response = self.client.post(
            "/api/aqi/record",
            json=payload,
            headers={"Authorization": "Bearer admin_token"}
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(len(self.db.added), 0)

        # When co is supplied along with co2_ppm, both are preserved separately
        valid_payload = self.get_valid_payload(co=1.5, co2_ppm=430.0)
        res_ok = self.client.post(
            "/api/aqi/record",
            json=valid_payload,
            headers={"Authorization": "Bearer admin_token"}
        )
        self.assertEqual(res_ok.status_code, 200)
        added_aqi = [x for x in self.db.added if isinstance(x, AirQualityData)]
        self.assertEqual(added_aqi[0].co, 1.5)
        self.assertEqual(added_aqi[0].co2_ppm, 430.0)


class TestAQIIngestionCityValidation(BaseAQITestCase):
    """Tests verifying city UUID format and DB existence checks."""

    def setUp(self):
        super().setUp()
        self.app.dependency_overrides[verify_firebase_token] = lambda: {"uid": self.admin_user.firebase_uid}

    def test_malformed_city_uuid_rejected_with_400(self):
        payload = self.get_valid_payload(city_id="not-a-valid-uuid-string")
        response = self.client.post(
            "/api/aqi/record",
            json=payload,
            headers={"Authorization": "Bearer admin_token"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "Invalid city_id format")
        self.assertEqual(len(self.db.added), 0)

    def test_nonexistent_city_rejected_with_404(self):
        missing_city_id = str(uuid.uuid4())
        payload = self.get_valid_payload(city_id=missing_city_id)
        response = self.client.post(
            "/api/aqi/record",
            json=payload,
            headers={"Authorization": "Bearer admin_token"}
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "City not found")
        self.assertEqual(len(self.db.added), 0)


class TestAQIPublicReadEndpoints(BaseAQITestCase):
    """Tests verifying that public city-level environmental read endpoints remain accessible unauthenticated."""

    def test_get_city_aqi_records_public(self):
        response = self.client.get(f"/api/aqi/city/{self.city_id}")
        self.assertEqual(response.status_code, 200)
        records = response.json()
        self.assertIsInstance(records, list)
        self.assertGreaterEqual(len(records), 1)
        self.assertEqual(records[0]["city_id"], str(self.city_id))
        self.assertEqual(records[0]["pm25"], 45.0)

    def test_get_city_aqi_records_invalid_uuid_returns_400(self):
        response = self.client.get("/api/aqi/city/not-a-valid-uuid")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "Invalid city_id format")

    def test_get_all_cities_latest_aqi_public(self):
        response = self.client.get("/api/aqi/cities")
        self.assertEqual(response.status_code, 200)
        cities = response.json()
        self.assertIsInstance(cities, list)
        self.assertGreaterEqual(len(cities), 1)
        self.assertEqual(cities[0]["name"], "Test City")
        self.assertEqual(cities[0]["aqi_score"], 95.0)

    @patch("routers.aqi.fetch_live_aqi")
    def test_get_city_latest_aqi_public(self, mock_fetch_live):
        mock_fetch_live.return_value = None
        response = self.client.get(f"/api/aqi/city/{self.city_id}/latest")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["city_id"], str(self.city_id))
        self.assertEqual(data["city_name"], "Test City")
        self.assertEqual(data["aqi_score"], 95.0)

    def test_get_city_latest_aqi_unknown_city_returns_404(self):
        missing_id = str(uuid.uuid4())
        response = self.client.get(f"/api/aqi/city/{missing_id}/latest")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "City not found")

    def test_get_live_status_public(self):
        response = self.client.get("/api/aqi/live-status")
        self.assertEqual(response.status_code, 200)
        self.assertIn("live_api", response.json())




class TestPollutantUnitContract(BaseAQITestCase):
    """Tests verifying the CPCB-compatible unit contract and CO sub-index calculation."""

    def test_co_subindex_uses_mg_m3_directly_without_1_145_scaling(self):
        """
        Verify that co_si() accepts mg/m3 directly.
        Under the old formula, co=1.0 was multiplied by 1.145 resulting in 57.25.
        Under the corrected CPCB formula, co=1.0 mg/m3 yields exactly 50.0.
        co=5.0 mg/m3 yields 137.5 (not 146.56).
        """
        # When CO is the dominant pollutant:
        # co = 1.0 mg/m3 -> sub-index is exactly 50.0
        aqi_1 = calculate_aqi(pm25=0, pm10=0, no2=0, co=1.0, so2=0, o3=0)
        self.assertEqual(aqi_1, 50.0)

        # co = 2.0 mg/m3 -> sub-index is exactly 100.0 (Satisfactory boundary)
        aqi_2 = calculate_aqi(pm25=0, pm10=0, no2=0, co=2.0, so2=0, o3=0)
        self.assertEqual(aqi_2, 100.0)

        # co = 5.0 mg/m3 -> sub-index: 100 + 100 * (5 - 2) / 8 = 137.5
        aqi_5 = calculate_aqi(pm25=0, pm10=0, no2=0, co=5.0, so2=0, o3=0)
        self.assertEqual(aqi_5, 137.5)

    def test_co_breakpoint_boundaries_and_interpolation(self):
        """Verify all CPCB CO breakpoint boundaries and interpolation points (in mg/m3)."""
        test_points = [
            (0.0, 0.0),       # Zero concentration -> 0 sub-index
            (-2.0, 0.0),      # Negative value clamped to 0
            (0.5, 25.0),      # Midpoint Good (0 - 1 mg/m3 -> 0 - 50)
            (1.0, 50.0),      # Good breakpoint
            (1.5, 75.0),      # Midpoint Satisfactory (1 - 2 mg/m3 -> 50 - 100)
            (2.0, 100.0),     # Satisfactory breakpoint
            (6.0, 150.0),     # Midpoint Moderate (2 - 10 mg/m3 -> 100 - 200)
            (10.0, 200.0),    # Moderate breakpoint
            (13.5, 250.0),    # Midpoint Poor (10 - 17 mg/m3 -> 200 - 300)
            (17.0, 300.0),    # Poor breakpoint
            (25.5, 350.0),    # Midpoint Very Poor (17 - 34 mg/m3 -> 300 - 400)
            (34.0, 400.0),    # Very Poor breakpoint
            (42.5, 450.0),    # Midpoint Severe (34 - 51 mg/m3 -> 400 - 500)
            (51.0, 500.0),    # Upper bound of Severe
            (100.0, 500.0),   # Capped at 500
        ]
        for co_val, expected_subindex in test_points:
            with self.subTest(co_mg_m3=co_val, expected=expected_subindex):
                subindex = calculate_aqi(0, 0, 0, co_val, 0, 0)
                self.assertAlmostEqual(subindex, expected_subindex, places=2)

    def test_openweather_normalization_converts_co_ug_to_mg_exactly_once(self):
        """
        OpenWeather returns CO in ug/m3.
        normalize_openweather_components must divide CO by 1000.0 exactly once to produce mg/m3.
        All other components must remain in ug/m3.
        """
        raw_components = {
            "pm2_5": 35.8,
            "pm10": 62.4,
            "no2": 28.5,
            "co": 2500.0,   # 2500 ug/m3 == 2.5 mg/m3
            "so2": 14.1,
            "o3": 44.0
        }
        normalized = normalize_openweather_components(raw_components)
        
        self.assertEqual(normalized["pm25"], 35.8)
        self.assertEqual(normalized["pm10"], 62.4)
        self.assertEqual(normalized["no2"], 28.5)
        self.assertEqual(normalized["co"], 2.5)       # Exactly 2500 / 1000.0
        self.assertEqual(normalized["so2"], 14.1)
        self.assertEqual(normalized["o3"], 44.0)

    def test_openweather_main_aqi_not_used_as_pollutant(self):
        """Verify that OpenWeather's proprietary 'main.aqi' index (1-5) is excluded from components."""
        raw_data = {
            "main": {"aqi": 3},
            "components": {
                "pm2_5": 20.0,
                "pm10": 40.0,
                "no2": 15.0,
                "co": 800.0,
                "so2": 10.0,
                "o3": 30.0
            }
        }
        normalized = normalize_openweather_components(raw_data["components"])
        self.assertNotIn("aqi", normalized)
        self.assertNotIn("main", normalized)
        self.assertEqual(normalized["co"], 0.8)

    def test_openweather_explicit_zero_preservation(self):
        """An explicit 0.0 measurement in OpenWeather data must not be converted to None."""
        raw_components = {
            "pm2_5": 10.0,
            "pm10": 20.0,
            "no2": 5.0,
            "co": 0.0,
            "so2": 0.0,
            "o3": 15.0
        }
        normalized = normalize_openweather_components(raw_components)
        self.assertEqual(normalized["co"], 0.0)
        self.assertEqual(normalized["so2"], 0.0)

    def test_manual_ingestion_calculates_aqi_using_direct_mg_m3_co(self):
        """
        When an admin posts a manual ingestion with co=2.0 mg/m3 and low other pollutants,
        the calculated AQI is 100.0 (CPCB Satisfactory boundary).
        """
        self.app.dependency_overrides[verify_firebase_token] = lambda: {"uid": self.admin_user.firebase_uid}
        payload = {
            "city_id": str(self.city_id),
            "pm25": 10.0,   # subindex = 16.67
            "pm10": 10.0,   # subindex = 10.0
            "no2": 10.0,    # subindex = 12.5
            "co": 2.0,      # subindex = 100.0 (dominant)
            "so2": 10.0,    # subindex = 12.5
            "o3": 10.0      # subindex = 10.0
        }
        response = self.client.post(
            "/api/aqi/record",
            json=payload,
            headers={"Authorization": "Bearer admin_token"}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["co"], 2.0)
        self.assertEqual(data["aqi_score"], 100.0)

    @patch("scheduler.SessionLocal")
    @patch("scheduler.fetch_live_aqi")
    def test_scheduler_persists_normalized_co_and_calculates_cpcb_aqi(self, mock_fetch, mock_session_factory):
        """Verify that scheduler uses fetch_live_aqi directly and stores normalized mg/m3 CO and CPCB AQI."""
        import asyncio
        from scheduler import refresh_all_cities
        
        mock_fetch.return_value = {
            "pm25": 30.0,
            "pm10": 50.0,
            "no2": 20.0,
            "co": 2.0,      # in mg/m3 -> sub-index is 100.0 (dominant)
            "so2": 15.0,
            "o3": 25.0,
            "source": "live"
        }
        mock_session = MagicMock()
        mock_session_factory.return_value = mock_session
        
        mock_city = MagicMock()
        mock_city.city_name = "Delhi"
        mock_city.city_id = uuid.uuid4()
        
        mock_rec = MagicMock()
        mock_rec.city_id = mock_city.city_id
        
        mock_session.query.return_value.filter.return_value.first.side_effect = [mock_city, mock_rec] * 10
        
        asyncio.run(refresh_all_cities())
        
        # Verify that rec.co was assigned normalized 2.0 mg/m3
        self.assertEqual(mock_rec.co, 2.0)
        # Verify that rec.aqi_score was computed using CPCB formula (100.0)
        self.assertEqual(mock_rec.aqi_score, 100.0)

    def test_other_pollutant_breakpoints_remain_unchanged(self):
        """Verify that PM2.5, PM10, NO2, SO2, O3 sub-indices are unaffected by the CO correction."""
        # PM2.5 breakpoint: 30 ug/m3 -> 50, 60 ug/m3 -> 100
        self.assertEqual(calculate_aqi(30, 0, 0, 0, 0, 0), 50.0)
        self.assertEqual(calculate_aqi(60, 0, 0, 0, 0, 0), 100.0)
        
        # PM10 breakpoint: 100 ug/m3 -> 100
        self.assertEqual(calculate_aqi(0, 100, 0, 0, 0, 0), 100.0)
        
        # NO2 breakpoint: 40 ug/m3 -> 50, 80 ug/m3 -> 100
        self.assertEqual(calculate_aqi(0, 0, 40, 0, 0, 0), 50.0)
        self.assertEqual(calculate_aqi(0, 0, 80, 0, 0, 0), 100.0)
        
        # SO2 breakpoint: 40 ug/m3 -> 50, 80 ug/m3 -> 100
        self.assertEqual(calculate_aqi(0, 0, 0, 0, 40, 0), 50.0)
        self.assertEqual(calculate_aqi(0, 0, 0, 0, 80, 0), 100.0)
        
        # O3 breakpoint: 100 ug/m3 -> 100
        self.assertEqual(calculate_aqi(0, 0, 0, 0, 0, 100), 100.0)


class TestManualAQISubmissionSemanticsAndProvenance(BaseAQITestCase):
    """
    Regression tests for Milestone 1, Iteration 1.5F:
    - Verifying manual submission semantics: authorization is not quality/compliance verification.
    - Missing averaging-period metadata does not default to CPCB compliance.
    - Correct declared averaging periods for 24h vs 8h pollutants.
    - Capturing observation time vs ingestion time.
    - Repeated scheduler runs not artificially refreshing old observations.
    """

    def setUp(self):
        super().setUp()
        self.app.dependency_overrides[verify_firebase_token] = lambda: {"uid": self.admin_user.firebase_uid}

    def test_manual_submission_missing_averaging_periods_is_unverified(self):
        """
        Missing averaging-period metadata must not silently default to a claim of CPCB compliance.
        Preserved as a distinctly labelled manual_submission input with is_cpcb_compliant=False
        and averaging_period_status='missing_declaration'.
        """
        payload = self.get_valid_payload()
        # Ensure averaging_periods is omitted
        payload.pop("averaging_periods", None)
        
        response = self.client.post(
            "/api/aqi/record",
            json=payload,
            headers={"Authorization": "Bearer admin_token"}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["calculation_status"], "unverified")
        self.assertEqual(data["calculation_type"], "manual_submission")
        self.assertFalse(data["is_cpcb_compliant"])
        self.assertEqual(data["averaging_period_status"], "missing_declaration")
        self.assertEqual(data["verification_status"], "user_declared_admin")
        self.assertIn("omitted", data["reason"].lower())

    def test_manual_submission_with_matching_cpcb_declared_periods(self):
        """
        When caller explicitly declares CPCB-compliant averaging periods (24h for PM2.5/PM10/NO2/SO2,
        8h for CO/O3), the response uses calculation_type='manual_submission', is_cpcb_compliant=False,
        calculation_formula='cpcb_formula', and averaging_period_status='declared_periods_match'.
        It does NOT claim independently verified CPCB compliance.
        """
        payload = self.get_valid_payload()
        payload["averaging_periods"] = {
            "pm25": "24h",
            "pm10": "24h",
            "no2":  "24h",
            "so2":  "24h",
            "co":   "8h",
            "o3":   "8h",
        }
        payload["source_provenance"] = "Admin_Certified_Station_1"

        response = self.client.post(
            "/api/aqi/record",
            json=payload,
            headers={"Authorization": "Bearer admin_token"}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["calculation_status"], "valid")
        self.assertEqual(data["calculation_type"], "manual_submission")
        self.assertEqual(data["calculation_formula"], "cpcb_formula")
        self.assertFalse(data["is_cpcb_compliant"])
        self.assertEqual(data["averaging_period_status"], "declared_periods_match")
        self.assertEqual(data["verification_status"], "user_declared_admin")
        self.assertEqual(data["provenance"], "Admin_Certified_Station_1")

    def test_manual_submission_with_invalid_declared_periods(self):
        """
        Declaring non-compliant averaging periods (e.g. 1h for PM2.5 or 24h for CO)
        must NOT be marked CPCB compliant and must have averaging_period_status='declared_periods_mismatch'.
        """
        payload = self.get_valid_payload()
        payload["averaging_periods"] = {
            "pm25": "1h",   # Invalid: CPCB requires 24h
            "pm10": "24h",
            "no2":  "24h",
            "so2":  "24h",
            "co":   "24h",  # Invalid: CPCB requires 8h
            "o3":   "8h",
        }
        response = self.client.post(
            "/api/aqi/record",
            json=payload,
            headers={"Authorization": "Bearer admin_token"}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["calculation_status"], "invalid_averaging_period")
        self.assertEqual(data["calculation_type"], "manual_submission")
        self.assertFalse(data["is_cpcb_compliant"])
        self.assertEqual(data["averaging_period_status"], "declared_periods_mismatch")
        self.assertIn("Declared averaging periods do not match", data["reason"])

    def test_manual_submission_omitted_observation_time_returns_null_observation_time(self):
        """
        When observation_time is omitted from the submission payload, response
        must return observation_time=None rather than substituting the ingestion time.
        """
        payload = self.get_valid_payload()
        payload.pop("observation_time", None)
        response = self.client.post(
            "/api/aqi/record",
            json=payload,
            headers={"Authorization": "Bearer admin_token"}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsNone(data["observation_time"])
        self.assertIsNotNone(data["ingested_at"])

    def test_read_endpoints_do_not_fabricate_historical_ingestion_time(self):
        """
        Database records persisted historically without separate ingested_at
        must return ingested_at=None and clearly preserve the genuine observation timestamp.
        """
        res = self.client.get(f"/api/aqi/city/{self.city_id}")
        self.assertEqual(res.status_code, 200)
        records = res.json()
        self.assertGreater(len(records), 0)
        first_rec = records[0]
        # In existing_record, recorded_at was persisted in the single-timestamp schema;
        # observation_time is None (source time unknown) and ingested_at is None (not fabricated)
        self.assertIsNone(first_rec["observation_time"])
        self.assertIsNone(first_rec["ingested_at"])
        self.assertIsNotNone(first_rec["recorded_at"])

        latest_res = self.client.get(f"/api/aqi/city/{self.city_id}/latest")
        self.assertEqual(latest_res.status_code, 200)
        latest_data = latest_res.json()
        self.assertIsNone(latest_data["observation_time"])
        self.assertIsNone(latest_data["ingested_at"])
        self.assertIsNotNone(latest_data["recorded_at"])

    def test_manual_submission_captures_observation_time_and_source_provenance(self):
        """
        Ensure source observation timestamp is preserved distinctly from ingestion timestamp,
        and user-declared provenance is explicitly captured.
        """
        payload = self.get_valid_payload()
        obs_time_str = "2026-10-31T09:15:00"
        payload["observation_time"] = obs_time_str
        payload["source_provenance"] = "Field_Audit_Sensor_Beta"

        response = self.client.post(
            "/api/aqi/record",
            json=payload,
            headers={"Authorization": "Bearer admin_token"}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsNotNone(data["observation_time"])
        self.assertIsNotNone(data["ingested_at"])
        self.assertIn("2026-10-31T09:15:00", data["observation_time"])
        self.assertEqual(data["provenance"], "Field_Audit_Sensor_Beta")

        # In database, record.recorded_at must reflect the source observation time
        added = [x for x in self.db.added if isinstance(x, AirQualityData)]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0].recorded_at.isoformat(), "2026-10-31T09:15:00")

    def test_client_supplied_aqi_score_remains_untrusted_with_manual_declarations(self):
        """
        Client-supplied aqi_score cannot hijack the calculation even when valid
        averaging periods and provenance are provided.
        """
        payload = self.get_valid_payload()
        payload["aqi_score"] = 999.0  # Bogus client score
        payload["averaging_periods"] = {
            "pm25": "24h", "pm10": "24h", "no2": "24h", "so2": "24h",
            "co": "8h", "o3": "8h"
        }
        response = self.client.post(
            "/api/aqi/record",
            json=payload,
            headers={"Authorization": "Bearer admin_token"}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertNotEqual(data["aqi_score"], 999.0)
        # Stored in DB must also be authoritative
        added = [x for x in self.db.added if isinstance(x, AirQualityData)]
        self.assertEqual(len(added), 1)
        self.assertNotEqual(added[0].aqi_score, 999.0)

    @patch("scheduler.fetch_live_aqi")
    def test_repeated_scheduler_run_does_not_artificially_refresh_observation_time(self, mock_fetch):
        """
        A repeated scheduler run on unchanged data must NOT artificially refresh
        the source observation timestamp with datetime.utcnow(), nor insert duplicate history.
        """
        import asyncio
        from scheduler import refresh_all_cities

        fixed_dt = 1762080000
        mock_fetch.return_value = {
            "pm25": 40.0,
            "pm10": 70.0,
            "no2": 30.0,
            "co": 1.2,
            "so2": 18.0,
            "o3": 45.0,
            "dt": fixed_dt,
            "source": "live"
        }

        # Mock database session with existing record that already has this recorded_at
        expected_obs_dt = datetime.fromtimestamp(fixed_dt, tz=timezone.utc).replace(tzinfo=None)
        existing_rec = AirQualityData(
            city_id=self.city_id,
            recorded_at=expected_obs_dt,
            pm25=40.0, pm10=70.0, no2=30.0, co=1.2, so2=18.0, o3=45.0
        )

        with patch("scheduler.SessionLocal") as mock_session_cls:
            mock_session = MagicMock()
            mock_session_cls.return_value = mock_session
            
            mock_city = MagicMock()
            mock_city.city_name = "Delhi"
            mock_city.city_id = self.city_id
            
            # First query returns city, second returns existing_rec
            mock_session.query.return_value.filter.return_value.first.side_effect = [mock_city, existing_rec] * 10
            
            asyncio.run(refresh_all_cities())

            # Verify that rec.recorded_at was NOT refreshed to datetime.utcnow()
            self.assertEqual(existing_rec.recorded_at, expected_obs_dt)
            # Verify that no duplicate AQIHistory was added since is_new_observation is False
            history_added = [args[0] for args, _ in mock_session.add.call_args_list if isinstance(args[0], AQIHistory)]
            self.assertEqual(len(history_added), 0)


if __name__ == "__main__":
    unittest.main()
