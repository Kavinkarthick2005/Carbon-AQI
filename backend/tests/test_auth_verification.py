import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Ensure backend directory is importable regardless of current working directory
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

# Provide a safe dummy DATABASE_URL if none is set in the test environment
os.environ.setdefault("DATABASE_URL", "postgresql://test_user:test_pass@localhost:5432/test_db")

# Prevent import-time DB table creation or real service-account loading when importing routers.auth
with (
    patch("sqlalchemy.sql.schema.MetaData.create_all"),
    patch.dict(os.environ, {"FIREBASE_CREDENTIALS_PATH": ""}, clear=False),
):
    import firebase_admin
    from firebase_admin import auth as firebase_auth
    from firebase_admin import exceptions as firebase_exceptions
    from fastapi import FastAPI, Depends
    from fastapi.testclient import TestClient
    from routers import auth as auth_router


class VerifyFirebaseTokenTests(unittest.IsolatedAsyncioTestCase):
    """Focused regression tests for Firebase authentication token verification."""

    async def test_missing_authorization_header_is_rejected(self):
        with patch.object(firebase_admin, "_apps", {"[DEFAULT]": object()}):
            for missing_value in (None, "", "   "):
                with self.subTest(authorization=missing_value):
                    with self.assertRaises(auth_router.HTTPException) as ctx:
                        await auth_router.verify_firebase_token(authorization=missing_value)
                    self.assertEqual(ctx.exception.status_code, 401)
                    self.assertEqual(ctx.exception.detail, "Authorization header missing")

    async def test_malformed_bearer_header_is_rejected(self):
        malformed_headers = [
            "Basic dXNlcjpwYXNz",
            "Token abc123",
            "BearerTokenWithoutSpace",
            "invalid-header-value",
            "Bearer token_part_one token_part_two",
        ]
        with patch.object(firebase_admin, "_apps", {"[DEFAULT]": object()}):
            for header in malformed_headers:
                with self.subTest(authorization=header):
                    with self.assertRaises(auth_router.HTTPException) as ctx:
                        await auth_router.verify_firebase_token(authorization=header)
                    self.assertEqual(ctx.exception.status_code, 401)
                    self.assertEqual(
                        ctx.exception.detail,
                        "Invalid authorization header format",
                    )

    async def test_empty_bearer_token_is_rejected(self):
        empty_bearer_headers = [
            "Bearer",
            "Bearer ",
            "Bearer     ",
            "bearer   ",
        ]
        with patch.object(firebase_admin, "_apps", {"[DEFAULT]": object()}):
            for header in empty_bearer_headers:
                with self.subTest(authorization=header):
                    with self.assertRaises(auth_router.HTTPException) as ctx:
                        await auth_router.verify_firebase_token(authorization=header)
                    self.assertEqual(ctx.exception.status_code, 401)
                    self.assertEqual(ctx.exception.detail, "Bearer token missing")

    async def test_uninitialized_firebase_admin_rejects_request_without_mock_user(self):
        with (
            patch.object(firebase_admin, "_apps", {}),
            patch.object(firebase_auth, "verify_id_token") as mock_verify,
        ):
            with self.assertRaises(auth_router.HTTPException) as ctx:
                await auth_router.verify_firebase_token(authorization="Bearer any-non-empty-token")

            self.assertEqual(ctx.exception.status_code, 503)
            self.assertEqual(
                ctx.exception.detail,
                "Authentication service is not configured or unavailable",
            )
            mock_verify.assert_not_called()

    async def test_invalid_or_expired_tokens_are_rejected_without_leaking_details(self):
        error_cases = [
            (
                firebase_auth.InvalidIdTokenError(
                    "Firebase ID token has incorrect 'aud' claim. Expected 'secret-project-123'."
                ),
                401,
                "Invalid authentication token",
            ),
            (
                firebase_auth.ExpiredIdTokenError("Token expired at 1700000000", cause=None),
                401,
                "Authentication token has expired",
            ),
            (
                firebase_auth.RevokedIdTokenError("Token has been revoked"),
                401,
                "Authentication token has been revoked",
            ),
            (
                firebase_auth.UserDisabledError("The user record is disabled"),
                401,
                "User account is disabled",
            ),
        ]

        with patch.object(firebase_admin, "_apps", {"[DEFAULT]": object()}):
            for exc, expected_status, expected_detail in error_cases:
                with self.subTest(exception_type=type(exc).__name__):
                    with patch.object(firebase_auth, "verify_id_token", side_effect=exc):
                        with self.assertRaises(auth_router.HTTPException) as ctx:
                            await auth_router.verify_firebase_token(
                                authorization="Bearer bad.jwt.token"
                            )
                        self.assertEqual(ctx.exception.status_code, expected_status)
                        self.assertEqual(ctx.exception.detail, expected_detail)
                        self.assertNotIn("secret-project-123", ctx.exception.detail)

    async def test_decoded_token_without_uid_is_rejected(self):
        with (
            patch.object(firebase_admin, "_apps", {"[DEFAULT]": object()}),
            patch.object(
                firebase_auth,
                "verify_id_token",
                return_value={"email": "missing_uid@example.com"},
            ),
        ):
            with self.assertRaises(auth_router.HTTPException) as ctx:
                await auth_router.verify_firebase_token(authorization="Bearer uidless.jwt.token")
            self.assertEqual(ctx.exception.status_code, 401)
            self.assertEqual(ctx.exception.detail, "Invalid authentication token")

    async def test_valid_token_returns_decoded_firebase_identity(self):
        expected_claims = {
            "uid": "firebase_user_987",
            "email": "verified.user@example.com",
            "name": "Verified User",
        }
        with (
            patch.object(firebase_admin, "_apps", {"[DEFAULT]": object()}),
            patch.object(
                firebase_auth,
                "verify_id_token",
                return_value=expected_claims,
            ) as mock_verify,
        ):
            result = await auth_router.verify_firebase_token(
                authorization="Bearer valid.firebase.jwt"
            )
            self.assertEqual(result, expected_claims)
            self.assertNotEqual(result.get("uid"), "dev_user_123")
            mock_verify.assert_called_once_with("valid.firebase.jwt")

    async def test_verification_service_failure_does_not_fall_back_to_mock_identity(self):
        service_failures = [
            firebase_auth.CertificateFetchError("Public key cert fetch timeout", cause=None),
            firebase_auth.ConfigurationNotFoundError("Project auth configuration missing"),
            firebase_auth.UnexpectedResponseError("Malformed response from Google certs endpoint"),
            firebase_exceptions.UnavailableError("Firebase Auth service unavailable"),
            firebase_exceptions.InternalError("Internal Firebase error"),
            ValueError("Failed to ascertain project ID from the credential or the environment."),
            RuntimeError("Unexpected runtime failure in token verifier"),
        ]

        with patch.object(firebase_admin, "_apps", {"[DEFAULT]": object()}):
            for failure in service_failures:
                with self.subTest(failure=type(failure).__name__):
                    with patch.object(firebase_auth, "verify_id_token", side_effect=failure):
                        with self.assertRaises(auth_router.HTTPException) as ctx:
                            await auth_router.verify_firebase_token(
                                authorization="Bearer some.jwt.token"
                            )
                        self.assertEqual(ctx.exception.status_code, 503)
                        self.assertIn("unavailable", ctx.exception.detail.lower())


class ProtectedRouteIntegrationTests(unittest.TestCase):
    """Verify FastAPI dependency behaviour on protected routes using TestClient."""

    def setUp(self):
        app = FastAPI()

        @app.get("/protected")
        async def protected_endpoint(
            token_data: dict = Depends(auth_router.verify_firebase_token),
        ):
            return token_data

        self.client = TestClient(app)

    def test_protected_endpoint_rejects_when_firebase_uninitialized(self):
        with patch.object(firebase_admin, "_apps", {}):
            response = self.client.get(
                "/protected",
                headers={"Authorization": "Bearer arbitrary-dev-token"},
            )
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("dev_user_123", response.text)

    def test_protected_endpoint_accepts_valid_firebase_token(self):
        claims = {"uid": "real_uid_001", "email": "real@example.com"}
        with (
            patch.object(firebase_admin, "_apps", {"[DEFAULT]": object()}),
            patch.object(firebase_auth, "verify_id_token", return_value=claims),
        ):
            response = self.client.get(
                "/protected",
                headers={"Authorization": "Bearer valid.jwt.token"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), claims)


if __name__ == "__main__":
    unittest.main()

