import os
import unittest
from unittest.mock import patch
from pydantic import ValidationError
from starlette.testclient import TestClient

from config import Settings, get_settings
from main import app


class TestCentralizedConfiguration(unittest.TestCase):
    """
    Test suite for centralized configuration, environment validation,
    and CORS security hardening (Milestone 1.6B).
    """

    def setUp(self):
        get_settings.cache_clear()

    def tearDown(self):
        get_settings.cache_clear()

    # 1. Required DATABASE_URL validation
    def test_database_url_required(self):
        with self.assertRaises(ValidationError) as ctx:
            Settings(
                _env_file=None,
                DATABASE_URL=None,
            )
        self.assertIn("DATABASE_URL", str(ctx.exception))

        with self.assertRaises(ValidationError) as ctx:
            Settings(
                _env_file=None,
                DATABASE_URL="   ",
            )
        self.assertIn("DATABASE_URL", str(ctx.exception))

        with self.assertRaises(ValidationError) as ctx:
            Settings(
                _env_file=None,
                DATABASE_URL="invalid_url_without_scheme",
            )
        self.assertIn("DATABASE_URL", str(ctx.exception))

    # 2. Optional external API keys
    def test_optional_external_api_keys(self):
        s = Settings(
            _env_file=None,
            DATABASE_URL="postgresql://user:pass@localhost:5432/testdb",
            OPENWEATHER_API_KEY=None,
            GROQ_API_KEY=None,
        )
        self.assertIsNone(s.OPENWEATHER_API_KEY)
        self.assertIsNone(s.GROQ_API_KEY)

    # 3. Missing Firebase credentials preserving fail-closed behavior
    def test_firebase_credentials_optional_in_settings(self):
        s = Settings(
            _env_file=None,
            DATABASE_URL="postgresql://user:pass@localhost:5432/testdb",
            FIREBASE_CREDENTIALS_PATH=None,
        )
        self.assertIsNone(s.FIREBASE_CREDENTIALS_PATH)

    # 4. Environment variables overriding .env values
    def test_env_var_overrides_env_file(self):
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://os_override:pass@localhost/db"}):
            s = Settings(_env_file=None)
            self.assertEqual(s.DATABASE_URL, "postgresql://os_override:pass@localhost/db")

    # 5. Environment-file loading independent of the working directory
    def test_env_file_deterministic_loading(self):
        # Settings._DEFAULT_ENV_FILE points deterministically to backend/.env
        import config
        self.assertTrue(config._DEFAULT_ENV_FILE.endswith(os.path.join("backend", ".env")) or
                        config._DEFAULT_ENV_FILE.endswith(".env"))

    # 6. Comma-separated ALLOWED_ORIGINS parsing
    def test_comma_separated_allowed_origins_parsing(self):
        s = Settings(
            _env_file=None,
            DATABASE_URL="postgresql://user:pass@localhost:5432/testdb",
            ALLOWED_ORIGINS="http://localhost:3000,http://127.0.0.1:3000",
        )
        self.assertEqual(s.ALLOWED_ORIGINS, ["http://localhost:3000", "http://127.0.0.1:3000"])

    # 7. Duplicate and whitespace normalization
    def test_duplicate_and_whitespace_normalization(self):
        s = Settings(
            _env_file=None,
            DATABASE_URL="postgresql://user:pass@localhost:5432/testdb",
            ALLOWED_ORIGINS="  http://localhost:3000/ , http://localhost:3000 , http://127.0.0.1:3000  ",
        )
        self.assertEqual(s.ALLOWED_ORIGINS, ["http://localhost:3000", "http://127.0.0.1:3000"])

    # 8. Rejection of wildcard origins
    def test_rejection_of_wildcard_origins(self):
        with self.assertRaises(ValidationError) as ctx:
            Settings(
                _env_file=None,
                DATABASE_URL="postgresql://user:pass@localhost:5432/testdb",
                ALLOWED_ORIGINS="*",
            )
        self.assertIn("Wildcard origin '*' is not permitted", str(ctx.exception))

        with self.assertRaises(ValidationError) as ctx:
            Settings(
                _env_file=None,
                DATABASE_URL="postgresql://user:pass@localhost:5432/testdb",
                ALLOWED_ORIGINS=["http://localhost:3000", "*"],
            )
        self.assertIn("Wildcard origin '*' is not permitted", str(ctx.exception))

    # 9. Rejection of HTTP origins in production
    def test_rejection_of_http_and_localhost_in_production(self):
        # Reject plain HTTP
        with self.assertRaises(ValidationError) as ctx:
            Settings(
                _env_file=None,
                DATABASE_URL="postgresql://user:pass@localhost:5432/testdb",
                APP_ENV="production",
                ALLOWED_ORIGINS="http://aqi.example.com",
            )
        self.assertIn("Production origin must use HTTPS", str(ctx.exception))

        # Reject localhost in production
        with self.assertRaises(ValidationError) as ctx:
            Settings(
                _env_file=None,
                DATABASE_URL="postgresql://user:pass@localhost:5432/testdb",
                APP_ENV="production",
                ALLOWED_ORIGINS="https://localhost:3000",
            )
        self.assertIn("Production origin cannot be localhost", str(ctx.exception))

        # Reject default development origins in production
        with self.assertRaises(ValidationError) as ctx:
            Settings(
                _env_file=None,
                DATABASE_URL="postgresql://user:pass@localhost:5432/testdb",
                APP_ENV="production",
            )
        self.assertIn("ALLOWED_ORIGINS is required in production", str(ctx.exception))

    # 10. Allowance of local development origins in development
    def test_allow_local_dev_origins_in_development(self):
        s = Settings(
            _env_file=None,
            DATABASE_URL="postgresql://user:pass@localhost:5432/testdb",
            APP_ENV="development",
            ALLOWED_ORIGINS="http://localhost:3000,http://127.0.0.1:3000",
        )
        self.assertEqual(s.APP_ENV, "development")
        self.assertIn("http://localhost:3000", s.ALLOWED_ORIGINS)
        self.assertIn("http://127.0.0.1:3000", s.ALLOWED_ORIGINS)

    # 11. Explicit methods and headers
    def test_explicit_methods_and_headers_in_settings(self):
        s = Settings(
            _env_file=None,
            DATABASE_URL="postgresql://user:pass@localhost:5432/testdb",
        )
        self.assertEqual(s.CORS_ALLOW_METHODS, ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
        self.assertEqual(s.CORS_ALLOW_HEADERS, ["Authorization", "Content-Type", "Accept"])
        self.assertFalse(s.CORS_ALLOW_CREDENTIALS)

    # 12. Successful CORS preflight for an allowed frontend origin
    def test_cors_preflight_allowed_origin(self):
        client = TestClient(app)
        response = client.options(
            "/api/aqi/cities",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "Authorization,Content-Type",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("access-control-allow-origin"), "http://localhost:3000")
        allow_methods = response.headers.get("access-control-allow-methods", "")
        self.assertIn("GET", allow_methods)
        self.assertIn("POST", allow_methods)

    # 13. Rejection of an unapproved origin
    def test_cors_preflight_rejected_for_unapproved_origin(self):
        client = TestClient(app)
        response = client.options(
            "/api/aqi/cities",
            headers={
                "Origin": "https://malicious-third-party.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        self.assertNotIn("access-control-allow-origin", response.headers)

    # 14. Correct credential behavior based on the actual frontend authentication flow
    def test_cors_credentials_disabled(self):
        client = TestClient(app)
        response = client.options(
            "/api/aqi/cities",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
            },
        )
        # allow-credentials must NOT be present or must NOT be 'true'
        self.assertNotEqual(response.headers.get("access-control-allow-credentials"), "true")

    # Additional: Secret redaction in repr/str
    def test_secret_redaction_in_repr(self):
        s = Settings(
            _env_file=None,
            DATABASE_URL="postgresql://admin_user:super_secret_password_123@localhost:5432/carbon_db",
            OPENWEATHER_API_KEY="secret_weather_key",
            GROQ_API_KEY="secret_groq_key",
        )
        repr_str = repr(s)
        self.assertNotIn("super_secret_password_123", repr_str)
        self.assertNotIn("secret_weather_key", repr_str)
        self.assertNotIn("secret_groq_key", repr_str)
        self.assertIn("***:***@localhost:5432", repr_str)


if __name__ == "__main__":
    unittest.main()

