import os
import sys
import unittest
import uuid
from datetime import datetime
from unittest.mock import MagicMock, patch

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

os.environ.setdefault("DATABASE_URL", "postgresql://test_user:test_pass@localhost:5432/test_db")

with (
    patch("sqlalchemy.sql.schema.MetaData.create_all"),
    patch.dict(os.environ, {"FIREBASE_CREDENTIALS_PATH": ""}, clear=False),
):
    import firebase_admin
    from firebase_admin import auth as firebase_auth
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from database import get_db
    from models import City, AirQualityData
    from routers import ml_predict, alerts as alerts_router


class SmartAnalysisSecurityTests(unittest.TestCase):
    """Regression tests verifying authentication and side-effect prevention on smart analysis."""

    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(ml_predict.router, prefix="/api/predict")
        self.client = TestClient(self.app)
        self.test_city_id = str(uuid.uuid4())

    def test_smart_analysis_rejects_missing_authorization(self):
        with (
            patch.object(firebase_admin, "_apps", {"[DEFAULT]": object()}),
            patch("routers.ml_predict.get_forecast") as mock_forecast,
            patch("routers.ml_predict.evaluate_and_alert") as mock_alert,
        ):
            response = self.client.get(f"/api/predict/smart-analysis/{self.test_city_id}")

            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json(), {"detail": "Authorization header missing"})
            mock_forecast.assert_not_called()
            mock_alert.assert_not_called()

    def test_smart_analysis_rejects_invalid_token_without_side_effects(self):
        with (
            patch.object(firebase_admin, "_apps", {"[DEFAULT]": object()}),
            patch.object(
                firebase_auth,
                "verify_id_token",
                side_effect=firebase_auth.InvalidIdTokenError("Invalid token signature"),
            ),
            patch("routers.ml_predict.get_forecast") as mock_forecast,
            patch("routers.ml_predict.evaluate_and_alert") as mock_alert,
        ):
            response = self.client.get(
                f"/api/predict/smart-analysis/{self.test_city_id}",
                headers={"Authorization": "Bearer bad.jwt.token"},
            )

            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json(), {"detail": "Invalid authentication token"})
            mock_forecast.assert_not_called()
            mock_alert.assert_not_called()

    def test_smart_analysis_rejects_when_firebase_uninitialized(self):
        with (
            patch.object(firebase_admin, "_apps", {}),
            patch("routers.ml_predict.get_forecast") as mock_forecast,
            patch("routers.ml_predict.evaluate_and_alert") as mock_alert,
        ):
            response = self.client.get(
                f"/api/predict/smart-analysis/{self.test_city_id}",
                headers={"Authorization": "Bearer some.jwt.token"},
            )

            self.assertEqual(response.status_code, 503)
            self.assertEqual(
                response.json(),
                {"detail": "Authentication service is not configured or unavailable"},
            )
            mock_forecast.assert_not_called()
            mock_alert.assert_not_called()

    def test_smart_analysis_accepts_valid_identity_and_executes_orchestration(self):
        mock_db = MagicMock()
        city_uuid = uuid.UUID(self.test_city_id)

        mock_city = MagicMock()
        mock_city.city_id = city_uuid
        mock_city.city_name = "Delhi"

        mock_aqi_record = MagicMock()
        mock_aqi_record.city_id = city_uuid
        mock_aqi_record.pm25 = 120.0
        mock_aqi_record.pm10 = 180.0
        mock_aqi_record.no2 = 35.0
        mock_aqi_record.co = 1.8
        mock_aqi_record.so2 = 25.0
        mock_aqi_record.o3 = 45.0
        mock_aqi_record.aqi_score = 220.0
        mock_aqi_record.recorded_at = datetime.utcnow()

        def query_side_effect(model):
            query_mock = MagicMock()
            if model is City:
                filter_mock = MagicMock()
                filter_mock.first.return_value = mock_city
                query_mock.filter.return_value = filter_mock
            elif model is AirQualityData:
                filter_mock = MagicMock()
                order_mock = MagicMock()
                order_mock.first.return_value = mock_aqi_record
                filter_mock.order_by.return_value = order_mock
                query_mock.filter.return_value = filter_mock
            return query_mock

        mock_db.query.side_effect = query_side_effect
        self.app.dependency_overrides[get_db] = lambda: mock_db

        mock_forecast_payload = {
            "city_id": self.test_city_id,
            "forecast": [{"day": 1, "predicted_aqi": 210.0, "risk_level": "Poor"}],
            "peak_aqi": 210.0,
            "peak_day": 1,
            "trend": "stable",
            "days_above_200": 1,
            "days_above_300": 0,
            "model": "xgboost_v1",
        }

        mock_strategy_payload = {
            "action_id": 1,
            "strategy_name": "Recommend Public Transport",
            "description": "Reduce vehicle emissions",
            "rl_reward": 1.0,
            "urgency": "warning",
            "trigger_reason": "AQI above 200",
            "forecast_driven": True,
        }

        mock_alerts_payload = [
            {
                "alert_id": str(uuid.uuid4()),
                "city_id": self.test_city_id,
                "city_name": "Delhi",
                "alert_type": "WARNING",
                "message": "Delhi - WARNING: AQI forecast above 200",
                "aqi_value": 220.0,
                "strategy_name": "Recommend Public Transport",
                "forecast_peak": 210.0,
                "triggered_at": "2026-10-29T18:00:00",
                "is_active": True,
            }
        ]

        try:
            with (
                patch.object(firebase_admin, "_apps", {"[DEFAULT]": object()}),
                patch.object(
                    firebase_auth,
                    "verify_id_token",
                    return_value={"uid": "authenticated_user_456"},
                ),
                patch("routers.ml_predict.fetch_live_aqi", return_value=None),
                patch("routers.ml_predict.get_forecast", return_value=mock_forecast_payload) as mock_get_forecast,
                patch("routers.ml_predict.get_enhanced_strategy", return_value=mock_strategy_payload),
                patch("routers.ml_predict.evaluate_and_alert", return_value=mock_alerts_payload) as mock_evaluate_alert,
            ):
                response = self.client.get(
                    f"/api/predict/smart-analysis/{self.test_city_id}",
                    headers={"Authorization": "Bearer valid.firebase.token"},
                )

                self.assertEqual(response.status_code, 200)
                data = response.json()
                self.assertEqual(data["city_id"], self.test_city_id)
                self.assertEqual(data["city_name"], "Delhi")
                self.assertEqual(data["current_aqi"], 220.0)
                self.assertIn("forecast", data)
                self.assertIn("rl_strategy", data)
                self.assertIn("active_alerts", data)
                self.assertEqual(data["active_alerts"], mock_alerts_payload)

                mock_get_forecast.assert_called_once_with(self.test_city_id)
                mock_evaluate_alert.assert_called_once()
        finally:
            self.app.dependency_overrides.pop(get_db, None)


class ActiveAlertsEndpointPolicyTests(unittest.TestCase):
    """Verify that GET /api/alerts/active is intentionally public and exposes only safe environmental alerts."""

    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(alerts_router.router, prefix="/api/alerts")
        self.client = TestClient(self.app)

        self.mock_public_alerts = [
            {
                "alert_id": str(uuid.uuid4()),
                "city_id": str(uuid.uuid4()),
                "city_name": "Mumbai",
                "alert_type": "WARNING",
                "message": "Mumbai - WARNING: AQI forecast above 200",
                "aqi_value": 205.0,
                "strategy_name": "Recommend Public Transport",
                "forecast_peak": 215.0,
                "triggered_at": "2026-10-29T18:00:00",
                "is_active": True,
            }
        ]

    def test_active_alerts_is_publicly_accessible_without_token(self):
        with patch(
            "routers.alerts.get_active_alerts_for_all_cities",
            return_value=self.mock_public_alerts,
        ) as mock_all:
            response = self.client.get("/api/alerts/active")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), self.mock_public_alerts)
            mock_all.assert_called_once()

    def test_active_alerts_by_city_is_publicly_accessible(self):
        city_id = str(uuid.uuid4())
        with patch(
            "routers.alerts.get_active_alerts_for_city",
            return_value=self.mock_public_alerts,
        ) as mock_city:
            response = self.client.get(f"/api/alerts/active?city_id={city_id}")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), self.mock_public_alerts)
            mock_city.assert_called_once_with(city_id)

    def test_active_alerts_payload_contains_only_public_environmental_data(self):
        allowed_public_fields = {
            "alert_id",
            "city_id",
            "city_name",
            "alert_type",
            "message",
            "aqi_value",
            "strategy_name",
            "forecast_peak",
            "triggered_at",
            "is_active",
        }
        forbidden_private_keywords = {"user", "email", "password", "hash", "secret", "token", "uid"}

        with patch(
            "routers.alerts.get_active_alerts_for_all_cities",
            return_value=self.mock_public_alerts,
        ):
            response = self.client.get("/api/alerts/active")
            alerts = response.json()

            self.assertIsInstance(alerts, list)
            for alert in alerts:
                # Every field present must be in the allowed public alert schema
                self.assertTrue(set(alert.keys()).issubset(allowed_public_fields))
                # No private user fields or credentials may appear
                for key in alert.keys():
                    self.assertFalse(
                        any(kw in key.lower() for kw in forbidden_private_keywords),
                        f"Found forbidden key: {key}",
                    )


if __name__ == "__main__":
    unittest.main()

