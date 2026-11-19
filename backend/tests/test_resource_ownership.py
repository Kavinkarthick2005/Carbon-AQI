"""
Regression tests for resource ownership, user data isolation, and preventing cross-user data access.
Verifies that:
1. User A can access their own private records.
2. User B cannot retrieve User A's private records.
3. User B cannot update User A's records through mutation endpoints.
4. History/list endpoints return only the authenticated caller's private records.
5. Client-supplied user_id parameters cannot override verified identity.
6. Unregistered Firebase identities cannot access private records or trigger silent provisioning.
7. Admin operations remain strictly protected.
8. Public endpoints (city listings, active alerts, carbon average) remain accessible.
"""

import os
import sys
import unittest
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

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
    from models import User, Feedback, CarbonFootprint, City, CityComparison
    from routers import auth as auth_router
    from routers import feedback as feedback_router
    from routers import carbon as carbon_router
    from routers import cities as cities_router
    from routers import alerts as alerts_router
    from routers.auth import verify_firebase_token, get_current_user


class ResourceOwnershipInMemoryDB:
    """In-memory database session for multi-user isolation tests."""

    def __init__(self, users=None, feedbacks=None, footprints=None, cities=None, comparisons=None):
        self.users = list(users or [])
        self.feedbacks = list(feedbacks or [])
        self.footprints = list(footprints or [])
        self.cities = list(cities or [])
        self.comparisons = list(comparisons or [])
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
                self._order_by_desc = False
                self._limit = None

            def outerjoin(self, target, onclause):
                self._joins.append((target, onclause))
                return self

            def join(self, target, onclause=None, isouter=False):
                self._joins.append((target, onclause))
                return self

            def filter(self, *conditions):
                self._filters.extend(conditions)
                return self

            def order_by(self, *clauses):
                return self

            def limit(self, n):
                self._limit = n
                return self

            def count(self):
                return len(self._get_matching_items())

            def scalar(self):
                # Simulated average for CarbonFootprint.total_emission
                all_fps = session.footprints + [x for x in session.added if isinstance(x, CarbonFootprint)]
                if not all_fps:
                    return 0.0
                return sum(fp.total_emission for fp in all_fps) / len(all_fps)

            def distinct(self):
                class DistinctHelper:
                    def count(inner):
                        all_fps = session.footprints + [x for x in session.added if isinstance(x, CarbonFootprint)]
                        return len({fp.user_id for fp in all_fps})
                return DistinctHelper()

            def _get_matching_items(self):
                target_cls = self.entities[0]
                if target_cls == User:
                    pool = session.users + [x for x in session.added if isinstance(x, User)]
                elif target_cls == Feedback:
                    pool = session.feedbacks + [x for x in session.added if isinstance(x, Feedback)]
                elif target_cls == CarbonFootprint:
                    pool = session.footprints + [x for x in session.added if isinstance(x, CarbonFootprint)]
                elif target_cls == City:
                    pool = session.cities + [x for x in session.added if isinstance(x, City)]
                elif target_cls == CityComparison:
                    pool = session.comparisons + [x for x in session.added if isinstance(x, CityComparison)]
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

                if self._limit:
                    results = results[:self._limit]
                return results

            def first(self):
                items = self._get_matching_items()
                return items[0] if items else None

            def all(self):
                if len(self.entities) == 2 and self.entities[0] == Feedback and self.entities[1] == User:
                    pairs = []
                    fb_pool = session.feedbacks + [x for x in session.added if isinstance(x, Feedback)]
                    user_pool = session.users + [x for x in session.added if isinstance(x, User)]
                    for fb in fb_pool:
                        matched_user = next((u for u in user_pool if u.user_id == fb.user_id), None)
                        pairs.append((fb, matched_user))
                    return pairs

                return self._get_matching_items()

        return QueryChain(entities)

    def add(self, instance):
        if isinstance(instance, Feedback):
            if not getattr(instance, "feedback_id", None):
                instance.feedback_id = uuid.uuid4()
            if not getattr(instance, "created_at", None):
                instance.created_at = datetime.now(timezone.utc)
        elif isinstance(instance, CarbonFootprint):
            if not getattr(instance, "footprint_id", None):
                instance.footprint_id = uuid.uuid4()
            if not getattr(instance, "calculated_at", None):
                instance.calculated_at = datetime.now(timezone.utc)
        elif isinstance(instance, CityComparison):
            if not getattr(instance, "comparison_id", None):
                instance.comparison_id = uuid.uuid4()
            if not getattr(instance, "compared_at", None):
                instance.compared_at = datetime.now(timezone.utc)
        elif isinstance(instance, User) and not getattr(instance, "user_id", None):
            instance.user_id = uuid.uuid4()

        self.added.append(instance)

    def commit(self):
        self.committed = True

    def refresh(self, instance):
        pass

    def rollback(self):
        self.rolled_back = True


class ResourceOwnershipIsolationTests(unittest.TestCase):
    """Test suite demonstrating cross-user isolation and resource ownership."""

    def setUp(self):
        # User A (Regular User)
        self.user_a = User(
            user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
            firebase_uid="firebase_uid_user_a",
            email="user_a@example.com",
            name="Alice",
            role="user",
            preferred_language="en",
        )
        # User B (Regular User)
        self.user_b = User(
            user_id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
            firebase_uid="firebase_uid_user_b",
            email="user_b@example.com",
            name="Bob",
            role="user",
            preferred_language="en",
        )
        # Admin User
        self.admin_user = User(
            user_id=uuid.UUID("99999999-9999-9999-9999-999999999999"),
            firebase_uid="firebase_uid_admin",
            email="admin@example.com",
            name="Admin Officer",
            role="admin",
            preferred_language="en",
        )

        # Alice's Feedback
        self.feedback_a = Feedback(
            feedback_id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
            user_id=self.user_a.user_id,
            subject="Alice's private issue",
            message="Sensors near my home seem offline",
            status="open",
            created_at=datetime.now(timezone.utc),
        )

        # Bob's Feedback
        self.feedback_b = Feedback(
            feedback_id=uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
            user_id=self.user_b.user_id,
            subject="Bob's suggestion",
            message="Add electric vehicle charging data",
            status="open",
            created_at=datetime.now(timezone.utc),
        )

        # Alice's Carbon Footprint
        self.carbon_a = CarbonFootprint(
            footprint_id=uuid.UUID("caaaaaaa-caaa-caaa-caaa-caaaaaaaaaaa"),
            user_id=self.user_a.user_id,
            transport_emission=120.5,
            energy_emission=85.0,
            diet_emission=60.0,
            total_emission=265.5,
            calculated_at=datetime.now(timezone.utc),
        )

        # Bob's Carbon Footprint
        self.carbon_b = CarbonFootprint(
            footprint_id=uuid.UUID("cbbbbbbb-cbbb-cbbb-cbbb-cbbbbbbbbbbb"),
            user_id=self.user_b.user_id,
            transport_emission=45.0,
            energy_emission=50.0,
            diet_emission=30.0,
            total_emission=125.0,
            calculated_at=datetime.now(timezone.utc),
        )

        self.db = ResourceOwnershipInMemoryDB(
            users=[self.user_a, self.user_b, self.admin_user],
            feedbacks=[self.feedback_a, self.feedback_b],
            footprints=[self.carbon_a, self.carbon_b],
            cities=[
                City(
                    city_id=uuid.UUID("c1111111-1111-1111-1111-111111111111"),
                    city_name="Delhi",
                    country="India",
                    latitude=28.61,
                    longitude=77.20,
                ),
                City(
                    city_id=uuid.UUID("c2222222-2222-2222-2222-222222222222"),
                    city_name="Mumbai",
                    country="India",
                    latitude=19.07,
                    longitude=72.87,
                ),
            ],
        )

        self.app = FastAPI()
        self.app.include_router(auth_router.router, prefix="/api/auth")
        self.app.include_router(feedback_router.router, prefix="/api/feedback")
        self.app.include_router(carbon_router.router, prefix="/api/carbon")
        self.app.include_router(cities_router.router, prefix="/api/cities")
        self.app.include_router(alerts_router.router, prefix="/api/alerts")

        self.app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(self.app)

    def tearDown(self):
        self.app.dependency_overrides.clear()

    # 1. User A can access their own private records
    def test_user_a_can_access_own_feedback(self):
        self.app.dependency_overrides[verify_firebase_token] = lambda: {"uid": self.user_a.firebase_uid}
        response = self.client.get("/api/feedback/my")
        self.assertEqual(response.status_code, 200)
        items = response.json()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["feedback_id"], str(self.feedback_a.feedback_id))
        self.assertEqual(items[0]["subject"], "Alice's private issue")

    def test_user_a_can_access_own_carbon_history(self):
        self.app.dependency_overrides[verify_firebase_token] = lambda: {"uid": self.user_a.firebase_uid}
        response = self.client.get("/api/carbon/history")
        self.assertEqual(response.status_code, 200)
        items = response.json()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["footprint_id"], str(self.carbon_a.footprint_id))
        self.assertEqual(items[0]["total_emission"], 265.5)

    def test_user_a_can_access_own_profile(self):
        self.app.dependency_overrides[verify_firebase_token] = lambda: {"uid": self.user_a.firebase_uid}
        response = self.client.get("/api/auth/me")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["user_id"], str(self.user_a.user_id))
        self.assertEqual(data["email"], "user_a@example.com")

    # 2. User B cannot retrieve User A's private records
    def test_user_b_cannot_see_user_a_feedback(self):
        self.app.dependency_overrides[verify_firebase_token] = lambda: {"uid": self.user_b.firebase_uid}
        response = self.client.get("/api/feedback/my")
        self.assertEqual(response.status_code, 200)
        items = response.json()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["feedback_id"], str(self.feedback_b.feedback_id))
        self.assertNotEqual(items[0]["feedback_id"], str(self.feedback_a.feedback_id))
        self.assertNotIn("Alice", str(items))

    def test_user_b_cannot_see_user_a_carbon_history(self):
        self.app.dependency_overrides[verify_firebase_token] = lambda: {"uid": self.user_b.firebase_uid}
        response = self.client.get("/api/carbon/history")
        self.assertEqual(response.status_code, 200)
        items = response.json()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["footprint_id"], str(self.carbon_b.footprint_id))
        self.assertNotEqual(items[0]["footprint_id"], str(self.carbon_a.footprint_id))

    # 3. User B cannot update User A's record through mutation endpoints
    def test_user_b_cannot_resolve_or_modify_user_a_feedback(self):
        self.app.dependency_overrides[verify_firebase_token] = lambda: {"uid": self.user_b.firebase_uid}
        # Attempt PATCH status
        response = self.client.patch(
            f"/api/feedback/{self.feedback_a.feedback_id}/status",
            json={"status": "resolved"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "Admin access required")
        self.assertEqual(self.feedback_a.status, "open")

        # Attempt PATCH resolve
        response_res = self.client.patch(f"/api/feedback/{self.feedback_a.feedback_id}/resolve")
        self.assertEqual(response_res.status_code, 403)
        self.assertEqual(self.feedback_a.status, "open")

    # 4. History/list endpoints return only the authenticated user's private records
    def test_history_endpoints_isolate_records_between_users(self):
        # Alice gets Alice's
        self.app.dependency_overrides[verify_firebase_token] = lambda: {"uid": self.user_a.firebase_uid}
        res_a = self.client.get("/api/carbon/history")
        self.assertEqual(len(res_a.json()), 1)
        self.assertEqual(res_a.json()[0]["total_emission"], 265.5)

        # Bob gets Bob's
        self.app.dependency_overrides[verify_firebase_token] = lambda: {"uid": self.user_b.firebase_uid}
        res_b = self.client.get("/api/carbon/history")
        self.assertEqual(len(res_b.json()), 1)
        self.assertEqual(res_b.json()[0]["total_emission"], 125.0)

    # 5. Supplying a different user_id cannot bypass ownership
    def test_client_supplied_user_id_in_query_ignored(self):
        self.app.dependency_overrides[verify_firebase_token] = lambda: {"uid": self.user_b.firebase_uid}
        # Bob passes Alice's user_id in query string
        response = self.client.get(f"/api/carbon/history?user_id={self.user_a.user_id}")
        self.assertEqual(response.status_code, 200)
        items = response.json()
        self.assertEqual(len(items), 1)
        # Still returns Bob's record, never Alice's
        self.assertEqual(items[0]["footprint_id"], str(self.carbon_b.footprint_id))

    def test_client_supplied_user_id_in_body_cannot_attribute_submission_to_another_user(self):
        self.app.dependency_overrides[verify_firebase_token] = lambda: {"uid": self.user_b.firebase_uid}
        # Bob tries to submit feedback with Alice's user_id in payload
        response = self.client.post(
            "/api/feedback/submit",
            json={
                "subject": "Spoofed feedback",
                "message": "Attacking user_id attribution",
                "user_id": str(self.user_a.user_id),
            },
        )
        self.assertEqual(response.status_code, 200)
        # Find the newly added feedback in DB
        added_feedback = [x for x in self.db.added if isinstance(x, Feedback) and x.subject == "Spoofed feedback"][0]
        # Must be attributed strictly to Bob (user_b), not Alice (user_a)
        self.assertEqual(added_feedback.user_id, self.user_b.user_id)
        self.assertNotEqual(added_feedback.user_id, self.user_a.user_id)

    # 6. Unregistered Firebase identity cannot access private user records
    def test_unregistered_identity_cannot_access_private_records(self):
        unregistered_uid = "unregistered_external_firebase_uid"
        self.app.dependency_overrides[verify_firebase_token] = lambda: {
            "uid": unregistered_uid,
            "email": "unregistered@example.com",
        }

        # Cannot read carbon history
        res_carbon = self.client.get("/api/carbon/history")
        self.assertEqual(res_carbon.status_code, 404)
        self.assertEqual(res_carbon.json()["detail"], "User not found")

        # Cannot read feedback
        res_fb = self.client.get("/api/feedback/my")
        self.assertEqual(res_fb.status_code, 404)
        self.assertEqual(res_fb.json()["detail"], "User not found")

        # Cannot submit feedback without registration
        res_submit = self.client.post(
            "/api/feedback/submit",
            json={"subject": "Unregistered feedback", "message": "Should fail"},
        )
        self.assertEqual(res_submit.status_code, 404)

        # Cannot read /me
        res_me = self.client.get("/api/auth/me")
        self.assertEqual(res_me.status_code, 404)

        # Confirm no user was silently created in database during these rejected reads
        created_users = [u for u in self.db.users + self.db.added if isinstance(u, User) and getattr(u, "firebase_uid", None) == unregistered_uid]
        self.assertEqual(len(created_users), 0)

    # 7. Admin-only operations remain protected
    def test_admin_operations_remain_protected(self):
        # Admin can view all feedback
        self.app.dependency_overrides[verify_firebase_token] = lambda: {"uid": self.admin_user.firebase_uid}
        res_admin = self.client.get("/api/feedback/all")
        self.assertEqual(res_admin.status_code, 200)
        self.assertEqual(len(res_admin.json()), 2)

        # Regular user cannot view all feedback
        self.app.dependency_overrides[verify_firebase_token] = lambda: {"uid": self.user_a.firebase_uid}
        res_user = self.client.get("/api/feedback/all")
        self.assertEqual(res_user.status_code, 403)

    # 8. Public city listings and alerts follow documented access policies
    def test_public_endpoints_remain_accessible(self):
        # Public city listings
        res_cities = self.client.get("/api/cities/list")
        self.assertEqual(res_cities.status_code, 200)
        self.assertEqual(len(res_cities.json()), 2)

        # Public carbon benchmark
        res_avg = self.client.get("/api/carbon/average")
        self.assertEqual(res_avg.status_code, 200)
        self.assertIn("average_monthly_emission_kg", res_avg.json())

        # Public active alerts
        with patch("routers.alerts.get_active_alerts_for_all_cities", return_value=[]):
            res_alerts = self.client.get("/api/alerts/active")
            self.assertEqual(res_alerts.status_code, 200)


if __name__ == "__main__":
    unittest.main()
