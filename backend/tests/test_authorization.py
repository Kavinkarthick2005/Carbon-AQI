"""
Unit and integration regression tests for role-based authorization and administrative access.
Verifies that administrative endpoints strictly enforce the 'admin' database role, reject
unauthenticated callers and non-admin authenticated users, prevent client-driven privilege
escalation, and allow legitimate administrators to perform admin actions.
"""

import os
import sys
import unittest
import uuid
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
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    from database import get_db
    from models import User, Feedback
    from routers.translation import Translation
    from routers import auth as auth_router
    from routers import feedback as feedback_router
    from routers import translation as translation_router
    from routers.auth import require_admin_user, check_admin_role, verify_firebase_token


class InMemoryDB:
    """Lightweight session simulation supporting User, Feedback, and Translation queries."""

    def __init__(self, users=None, feedbacks=None, translations=None):
        self.users = list(users or [])
        self.feedbacks = list(feedbacks or [])
        self.translations = list(translations or [])
        self.added = []
        self.committed = False
        self.rolled_back = False

    def query(self, *entities):
        session = self

        class QueryChain:
            def __init__(self, entities):
                self.entities = entities
                self._filters = []
                self._joins = []
                self._distinct = False

            def outerjoin(self, target, onclause):
                self._joins.append((target, onclause))
                return self

            def filter(self, *conditions):
                self._filters.extend(conditions)
                return self

            def order_by(self, *clauses):
                return self

            def distinct(self):
                self._distinct = True
                return self

            def count(self):
                return len(self._get_matching_items())

            def _get_matching_items(self):
                target_cls = self.entities[0]
                if target_cls == User:
                    pool = session.users + [x for x in session.added if isinstance(x, User)]
                elif target_cls == Feedback:
                    pool = session.feedbacks + [x for x in session.added if isinstance(x, Feedback)]
                elif target_cls == Translation:
                    pool = session.translations + [x for x in session.added if isinstance(x, Translation)]
                elif hasattr(target_cls, "class_") and target_cls.class_ == Translation:
                    pool = session.translations + [x for x in session.added if isinstance(x, Translation)]
                else:
                    pool = []

                results = pool
                for cond in self._filters:
                    # Evaluate simple binary conditions
                    left = getattr(cond, "left", None)
                    right = getattr(cond, "right", None)
                    key = getattr(left, "key", None) or getattr(left, "name", None)
                    val = getattr(right, "value", None)
                    if key is not None:
                        results = [item for item in results if getattr(item, key, None) == val]
                return results

            def first(self):
                items = self._get_matching_items()
                return items[0] if items else None

            def all(self):
                if len(self.entities) == 2 and self.entities[0] == Feedback and self.entities[1] == User:
                    # Outer join simulation between Feedback and User
                    pairs = []
                    fb_pool = session.feedbacks + [x for x in session.added if isinstance(x, Feedback)]
                    user_pool = session.users + [x for x in session.added if isinstance(x, User)]
                    for fb in fb_pool:
                        matched_user = next((u for u in user_pool if u.user_id == fb.user_id), None)
                        pairs.append((fb, matched_user))
                    return pairs

                # Distinct column query (e.g. Translation.key_name)
                if hasattr(self.entities[0], "key") and self.entities[0].key == "key_name":
                    all_trans = session.translations + [x for x in session.added if isinstance(x, Translation)]
                    keys = list({t.key_name for t in all_trans})
                    return [(k,) for k in keys]

                return self._get_matching_items()

        return QueryChain(entities)

    def add(self, instance):
        if isinstance(instance, Feedback):
            if not getattr(instance, "feedback_id", None):
                instance.feedback_id = uuid.uuid4()
            if not getattr(instance, "created_at", None):
                instance.created_at = datetime.now(timezone.utc)
        elif isinstance(instance, Translation) and not getattr(instance, "translation_id", None):
            instance.translation_id = uuid.uuid4()
        elif isinstance(instance, User) and not getattr(instance, "user_id", None):
            instance.user_id = uuid.uuid4()
        self.added.append(instance)

    def commit(self):
        self.committed = True

    def refresh(self, instance):
        pass

    def rollback(self):
        self.rolled_back = True


class AdminDependencyUnitTests(unittest.TestCase):
    """Unit tests for the require_admin_user dependency function."""

    def test_check_admin_role_alias_points_to_require_admin_user(self):
        self.assertIs(check_admin_role, require_admin_user)

    def test_require_admin_user_rejects_missing_uid_in_token_data(self):
        db = InMemoryDB()
        with self.assertRaises(HTTPException) as ctx:
            require_admin_user(token_data={}, db=db)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("missing required claims", ctx.exception.detail)

    def test_require_admin_user_rejects_non_dict_token_data(self):
        db = InMemoryDB()
        with self.assertRaises(HTTPException) as ctx:
            require_admin_user(token_data=None, db=db)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_require_admin_user_rejects_unregistered_user(self):
        db = InMemoryDB(users=[])
        with self.assertRaises(HTTPException) as ctx:
            require_admin_user(token_data={"uid": "unregistered_uid"}, db=db)
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(ctx.exception.detail, "User not found")

    def test_require_admin_user_rejects_regular_user_with_403(self):
        user = User(
            user_id=uuid.uuid4(),
            firebase_uid="regular_uid_123",
            email="regular@example.com",
            name="Regular User",
            role="user",
        )
        db = InMemoryDB(users=[user])
        with self.assertRaises(HTTPException) as ctx:
            require_admin_user(token_data={"uid": "regular_uid_123"}, db=db)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.detail, "Admin access required")

    def test_require_admin_user_succeeds_for_admin_user(self):
        admin = User(
            user_id=uuid.uuid4(),
            firebase_uid="admin_uid_999",
            email="admin@example.com",
            name="Admin User",
            role="admin",
        )
        db = InMemoryDB(users=[admin])
        returned_user = require_admin_user(token_data={"uid": "admin_uid_999"}, db=db)
        self.assertEqual(returned_user.user_id, admin.user_id)
        self.assertEqual(returned_user.role, "admin")


class AdminAuthorizationIntegrationTests(unittest.TestCase):
    """Integration tests verifying admin-only endpoints, standard user routes, and public routes."""

    def setUp(self):
        self.regular_user = User(
            user_id=uuid.uuid4(),
            firebase_uid="regular_user_uid",
            email="user@example.com",
            name="Standard User",
            role="user",
        )
        self.admin_user = User(
            user_id=uuid.uuid4(),
            firebase_uid="admin_user_uid",
            email="admin@example.com",
            name="System Admin",
            role="admin",
        )
        self.feedback_item = Feedback(
            feedback_id=uuid.UUID("11111111-2222-3333-4444-555555555555"),
            user_id=self.regular_user.user_id,
            subject="Bug report",
            message="Data missing for Delhi",
            status="open",
            created_at=datetime.now(timezone.utc),
        )
        self.db = InMemoryDB(
            users=[self.regular_user, self.admin_user],
            feedbacks=[self.feedback_item],
            translations=[
                Translation(
                    translation_id=uuid.uuid4(),
                    key_name="nav_dashboard",
                    language_code="en",
                    translated_text="Dashboard",
                )
            ],
        )

        self.app = FastAPI()
        self.app.include_router(feedback_router.router, prefix="/api/feedback")
        self.app.include_router(translation_router.router, prefix="/api/translation")
        self.app.include_router(auth_router.router, prefix="/api/auth")

        self.app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(self.app)

    def tearDown(self):
        self.app.dependency_overrides.clear()

    # 1. Unauthenticated requests to admin endpoints return 401
    def test_admin_feedback_all_rejects_unauthenticated_request_with_401(self):
        response = self.client.get("/api/feedback/all")
        self.assertEqual(response.status_code, 401)

    def test_admin_translation_add_rejects_unauthenticated_request_with_401(self):
        response = self.client.post(
            "/api/translation/add",
            json={"key_name": "test_key", "language_code": "en", "translated_text": "Test"},
        )
        self.assertEqual(response.status_code, 401)

    # 2. Authenticated non-admin users receive 403 Forbidden
    def test_admin_feedback_all_rejects_non_admin_with_403(self):
        self.app.dependency_overrides[verify_firebase_token] = lambda: {"uid": self.regular_user.firebase_uid}
        response = self.client.get(
            "/api/feedback/all",
            headers={"Authorization": "Bearer valid_regular_token"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "Admin access required")

    def test_admin_feedback_status_rejects_non_admin_with_403(self):
        self.app.dependency_overrides[verify_firebase_token] = lambda: {"uid": self.regular_user.firebase_uid}
        response = self.client.patch(
            f"/api/feedback/{self.feedback_item.feedback_id}/status",
            json={"status": "resolved"},
            headers={"Authorization": "Bearer valid_regular_token"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "Admin access required")

    def test_admin_feedback_resolve_rejects_non_admin_with_403(self):
        self.app.dependency_overrides[verify_firebase_token] = lambda: {"uid": self.regular_user.firebase_uid}
        response = self.client.patch(
            f"/api/feedback/{self.feedback_item.feedback_id}/resolve",
            headers={"Authorization": "Bearer valid_regular_token"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "Admin access required")

    def test_admin_translation_add_rejects_non_admin_with_403(self):
        self.app.dependency_overrides[verify_firebase_token] = lambda: {"uid": self.regular_user.firebase_uid}
        response = self.client.post(
            "/api/translation/add",
            json={"key_name": "new_key", "language_code": "ta", "translated_text": "புதியது"},
            headers={"Authorization": "Bearer valid_regular_token"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "Admin access required")

    # 3. Authenticated admin users succeed with 200 OK
    def test_admin_feedback_all_succeeds_for_admin_user(self):
        self.app.dependency_overrides[verify_firebase_token] = lambda: {"uid": self.admin_user.firebase_uid}
        response = self.client.get(
            "/api/feedback/all",
            headers={"Authorization": "Bearer valid_admin_token"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["subject"], "Bug report")
        self.assertEqual(data[0]["user_email"], "user@example.com")

    def test_admin_feedback_status_update_succeeds_for_admin_user(self):
        self.app.dependency_overrides[verify_firebase_token] = lambda: {"uid": self.admin_user.firebase_uid}
        response = self.client.patch(
            f"/api/feedback/{self.feedback_item.feedback_id}/status",
            json={"status": "resolved"},
            headers={"Authorization": "Bearer valid_admin_token"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("resolved", response.json()["message"])
        self.assertEqual(self.feedback_item.status, "resolved")
        self.assertTrue(self.db.committed)

    def test_admin_feedback_resolve_succeeds_for_admin_user(self):
        self.app.dependency_overrides[verify_firebase_token] = lambda: {"uid": self.admin_user.firebase_uid}
        response = self.client.patch(
            f"/api/feedback/{self.feedback_item.feedback_id}/resolve",
            headers={"Authorization": "Bearer valid_admin_token"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("resolved", response.json()["message"])
        self.assertEqual(self.feedback_item.status, "resolved")

    def test_admin_translation_add_succeeds_for_admin_user(self):
        self.app.dependency_overrides[verify_firebase_token] = lambda: {"uid": self.admin_user.firebase_uid}
        payload = {"key_name": "welcome_title", "language_code": "en", "translated_text": "Welcome"}
        response = self.client.post(
            "/api/translation/add",
            json=payload,
            headers={"Authorization": "Bearer valid_admin_token"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("successfully", response.json()["message"])
        self.assertTrue(self.db.committed)

    # 4. Privilege escalation prevention
    def test_client_cannot_elevate_privilege_via_body_claims(self):
        """Even if client sends role: admin or fake admin uid in payload, authorization relies strictly on DB role."""
        self.app.dependency_overrides[verify_firebase_token] = lambda: {"uid": self.regular_user.firebase_uid}
        response = self.client.post(
            "/api/translation/add",
            json={
                "key_name": "exploit_key",
                "language_code": "en",
                "translated_text": "exploit",
                "role": "admin",
                "firebase_uid": self.admin_user.firebase_uid,
            },
            headers={"Authorization": "Bearer valid_regular_token"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "Admin access required")

    def test_registration_does_not_honor_client_supplied_admin_role(self):
        """User registration ignores client-supplied role and provisions role='user'."""
        new_uid = "new_attempted_admin_uid"
        self.app.dependency_overrides[verify_firebase_token] = lambda: {
            "uid": new_uid,
            "email": "attacker@example.com",
            "name": "Attacker",
        }
        response = self.client.post(
            "/api/auth/register",
            json={"display_name": "Attacker", "role": "admin"},
            headers={"Authorization": "Bearer token"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["role"], "user")

    # 5. Non-admin routes allow standard users
    def test_regular_user_can_submit_feedback(self):
        self.app.dependency_overrides[verify_firebase_token] = lambda: {
            "uid": self.regular_user.firebase_uid,
            "email": self.regular_user.email,
        }
        response = self.client.post(
            "/api/feedback/submit",
            json={"subject": "Suggestion", "message": "Add more sensors"},
            headers={"Authorization": "Bearer valid_token"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["subject"], "Suggestion")

    def test_regular_user_can_view_own_feedback(self):
        self.app.dependency_overrides[verify_firebase_token] = lambda: {
            "uid": self.regular_user.firebase_uid,
            "email": self.regular_user.email,
        }
        response = self.client.get(
            "/api/feedback/my",
            headers={"Authorization": "Bearer valid_token"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 1)

    # 6. Public routes remain accessible without credentials
    def test_translation_keys_is_publicly_accessible(self):
        response = self.client.get("/api/translation/keys")
        self.assertEqual(response.status_code, 200)
        self.assertIn("keys", response.json())

    def test_translation_language_is_publicly_accessible(self):
        response = self.client.get("/api/translation/en")
        self.assertEqual(response.status_code, 200)
        self.assertIn("nav_dashboard", response.json())


if __name__ == "__main__":
    unittest.main()
