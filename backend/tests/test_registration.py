import os
import sys
import unittest
import uuid
from unittest.mock import patch

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
    from models import User
    from routers import auth as auth_router


class FakeSession:
    """Lightweight in-memory DB session that accurately simulates User queries and commits."""

    def __init__(self, initial_users=None, should_fail_commit=False):
        self.users = list(initial_users or [])
        self.added = []
        self.committed = False
        self.rolled_back = False
        self.should_fail_commit = should_fail_commit

    def query(self, model):
        class QueryHelper:
            def __init__(helper, session):
                helper.session = session

            def filter(helper, condition):
                key = getattr(condition.left, "key", None) or getattr(condition.left, "name", None)
                val = getattr(condition.right, "value", None)
                matched = [
                    u for u in (helper.session.users + helper.session.added)
                    if getattr(u, key, None) == val
                ]

                class FilterHelper:
                    def first(inner):
                        return matched[0] if matched else None
                return FilterHelper()
        return QueryHelper(self)

    def add(self, instance):
        if not getattr(instance, "user_id", None):
            instance.user_id = uuid.uuid4()
        self.added.append(instance)

    def commit(self):
        if self.should_fail_commit:
            raise Exception("Simulated DB connection drop")
        self.committed = True

    def refresh(self, instance):
        pass

    def rollback(self):
        self.rolled_back = True


class SecureRegistrationEndpointTests(unittest.TestCase):
    """Regression tests for authenticated and secure Firebase account provisioning."""

    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(auth_router.router, prefix="/api/auth")
        self.client = TestClient(self.app)

    def test_registration_rejects_missing_authorization(self):
        with patch.object(firebase_admin, "_apps", {"[DEFAULT]": object()}):
            response = self.client.post("/api/auth/register", json={"display_name": "Test User"})
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json(), {"detail": "Authorization header missing"})

    def test_registration_rejects_invalid_token(self):
        with (
            patch.object(firebase_admin, "_apps", {"[DEFAULT]": object()}),
            patch.object(
                firebase_auth,
                "verify_id_token",
                side_effect=firebase_auth.InvalidIdTokenError("Invalid token signature"),
            ),
        ):
            response = self.client.post(
                "/api/auth/register",
                headers={"Authorization": "Bearer bad.token"},
                json={"display_name": "Test User"},
            )
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json(), {"detail": "Invalid authentication token"})

    def test_registration_provisions_user_from_verified_claims(self):
        fake_db = FakeSession()
        self.app.dependency_overrides[get_db] = lambda: fake_db

        verified_claims = {
            "uid": "verified_firebase_uid_123",
            "email": "verified.user@example.com",
            "name": "Firebase Token Name",
        }

        try:
            with (
                patch.object(firebase_admin, "_apps", {"[DEFAULT]": object()}),
                patch.object(firebase_auth, "verify_id_token", return_value=verified_claims),
            ):
                # Client attempts to submit conflicting identity and plaintext password in request body
                malicious_client_payload = {
                    "firebase_uid": "spoofed_uid_999",
                    "email": "spoofed@attacker.com",
                    "password": "plaintext_super_secret_password",
                    "display_name": "Custom Profile Name",
                }

                response = self.client.post(
                    "/api/auth/register",
                    headers={"Authorization": "Bearer valid.firebase.token"},
                    json=malicious_client_payload,
                )

                self.assertEqual(response.status_code, 200)
                data = response.json()

                # Identity is derived strictly from the verified Firebase token
                self.assertEqual(data["email"], "verified.user@example.com")
                self.assertEqual(data["name"], "Custom Profile Name")
                self.assertNotIn("password", data)
                self.assertNotIn("password_hash", data)

                # Database row assertions
                self.assertEqual(len(fake_db.added), 1)
                user_record = fake_db.added[0]
                self.assertEqual(user_record.firebase_uid, "verified_firebase_uid_123")
                self.assertEqual(user_record.email, "verified.user@example.com")
                self.assertEqual(user_record.name, "Custom Profile Name")
                # Plaintext password is never persisted
                self.assertNotEqual(user_record.password_hash, "plaintext_super_secret_password")
                self.assertEqual(user_record.password_hash, "firebase_auth")
                self.assertTrue(fake_db.committed)
        finally:
            self.app.dependency_overrides.pop(get_db, None)

    def test_registration_is_idempotent_for_existing_user(self):
        existing_user = User(
            user_id=uuid.uuid4(),
            email="existing.user@example.com",
            name="Existing Name",
            firebase_uid="existing_firebase_uid",
            password_hash="firebase_auth",
            preferred_language="en",
            role="user",
        )
        fake_db = FakeSession(initial_users=[existing_user])
        self.app.dependency_overrides[get_db] = lambda: fake_db

        verified_claims = {
            "uid": "existing_firebase_uid",
            "email": "existing.user@example.com",
        }

        try:
            with (
                patch.object(firebase_admin, "_apps", {"[DEFAULT]": object()}),
                patch.object(firebase_auth, "verify_id_token", return_value=verified_claims),
            ):
                response = self.client.post(
                    "/api/auth/register",
                    headers={"Authorization": "Bearer valid.firebase.token"},
                    json={},
                )

                self.assertEqual(response.status_code, 200)
                data = response.json()
                self.assertEqual(data["email"], "existing.user@example.com")
                self.assertEqual(data["name"], "Existing Name")
                # Idempotent: no new user was created
                self.assertEqual(len(fake_db.added), 0)
        finally:
            self.app.dependency_overrides.pop(get_db, None)

    def test_registration_rejects_email_conflict_with_different_uid(self):
        existing_user_with_same_email = User(
            user_id=uuid.uuid4(),
            email="conflict@example.com",
            name="Original User",
            firebase_uid="legitimate_uid_aaa",
            password_hash="firebase_auth",
            preferred_language="en",
            role="user",
        )
        fake_db = FakeSession(initial_users=[existing_user_with_same_email])
        self.app.dependency_overrides[get_db] = lambda: fake_db

        attacker_claims = {
            "uid": "different_uid_bbb",
            "email": "conflict@example.com",
        }

        try:
            with (
                patch.object(firebase_admin, "_apps", {"[DEFAULT]": object()}),
                patch.object(firebase_auth, "verify_id_token", return_value=attacker_claims),
            ):
                response = self.client.post(
                    "/api/auth/register",
                    headers={"Authorization": "Bearer attacker.firebase.token"},
                    json={"display_name": "Attacker"},
                )

                self.assertEqual(response.status_code, 409)
                self.assertIn("already exists under a different identity", response.json()["detail"])
                self.assertEqual(len(fake_db.added), 0)
        finally:
            self.app.dependency_overrides.pop(get_db, None)

    def test_registration_rejects_missing_uid_or_email_claims(self):
        fake_db = FakeSession()
        self.app.dependency_overrides[get_db] = lambda: fake_db

        try:
            with patch.object(firebase_admin, "_apps", {"[DEFAULT]": object()}):
                # Case 1: Missing email claim
                with patch.object(
                    firebase_auth,
                    "verify_id_token",
                    return_value={"uid": "valid_uid_no_email"},
                ):
                    response = self.client.post(
                        "/api/auth/register",
                        headers={"Authorization": "Bearer token.missing.email"},
                        json={},
                    )
                    self.assertEqual(response.status_code, 400)
                    self.assertIn("missing required claims", response.json()["detail"])
                    self.assertEqual(len(fake_db.added), 0)

                # Case 2: Missing uid claim
                with patch.object(
                    firebase_auth,
                    "verify_id_token",
                    return_value={"email": "valid@example.com"},
                ):
                    response = self.client.post(
                        "/api/auth/register",
                        headers={"Authorization": "Bearer token.missing.uid"},
                        json={},
                    )
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(len(fake_db.added), 0)
        finally:
            self.app.dependency_overrides.pop(get_db, None)

    def test_registration_rolls_back_and_returns_500_on_db_error(self):
        fake_db = FakeSession(should_fail_commit=True)
        self.app.dependency_overrides[get_db] = lambda: fake_db

        verified_claims = {
            "uid": "new_uid_789",
            "email": "new.user@example.com",
        }

        try:
            with (
                patch.object(firebase_admin, "_apps", {"[DEFAULT]": object()}),
                patch.object(firebase_auth, "verify_id_token", return_value=verified_claims),
            ):
                response = self.client.post(
                    "/api/auth/register",
                    headers={"Authorization": "Bearer valid.token"},
                    json={"display_name": "Test User"},
                )

                self.assertEqual(response.status_code, 500)
                self.assertEqual(response.json(), {"detail": "Failed to provision user account"})
                # Internal exception string must not leak
                self.assertNotIn("Simulated DB connection drop", response.text)
                self.assertTrue(fake_db.rolled_back)
        finally:
            self.app.dependency_overrides.pop(get_db, None)


if __name__ == "__main__":
    unittest.main()
