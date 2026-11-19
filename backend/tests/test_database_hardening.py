import unittest
from unittest.mock import MagicMock, patch
from pydantic import ValidationError
from sqlalchemy.pool import StaticPool, QueuePool

from config import Settings, get_settings, mask_database_url
from database import create_app_engine, get_db, check_database_health, engine


class TestDatabaseHardening(unittest.TestCase):
    """
    Test suite for SQLAlchemy database engine, connection pool,
    and session lifecycle hardening (Milestone 1.6C).
    """

    def setUp(self):
        get_settings.cache_clear()

    def tearDown(self):
        get_settings.cache_clear()

    # 1. Pool settings validation in Settings
    def test_default_pool_settings(self):
        s = Settings(
            _env_file=None,
            DATABASE_URL="postgresql://user:pass@localhost:5432/testdb",
        )
        self.assertEqual(s.DB_POOL_SIZE, 5)
        self.assertEqual(s.DB_MAX_OVERFLOW, 10)
        self.assertEqual(s.DB_POOL_TIMEOUT, 30)
        self.assertEqual(s.DB_POOL_RECYCLE, 1800)

    def test_custom_pool_settings_coercion(self):
        s = Settings(
            _env_file=None,
            DATABASE_URL="postgresql://user:pass@localhost:5432/testdb",
            DB_POOL_SIZE="8",
            DB_MAX_OVERFLOW="15",
            DB_POOL_TIMEOUT="45",
            DB_POOL_RECYCLE="3600",
        )
        self.assertEqual(s.DB_POOL_SIZE, 8)
        self.assertEqual(s.DB_MAX_OVERFLOW, 15)
        self.assertEqual(s.DB_POOL_TIMEOUT, 45)
        self.assertEqual(s.DB_POOL_RECYCLE, 3600)

    def test_pool_size_validation_rejects_invalid_values(self):
        # 0 is invalid
        with self.assertRaises(ValidationError) as ctx:
            Settings(
                _env_file=None,
                DATABASE_URL="postgresql://user:pass@localhost:5432/testdb",
                DB_POOL_SIZE=0,
            )
        self.assertIn("DB_POOL_SIZE", str(ctx.exception))

        # Negative is invalid
        with self.assertRaises(ValidationError) as ctx:
            Settings(
                _env_file=None,
                DATABASE_URL="postgresql://user:pass@localhost:5432/testdb",
                DB_POOL_SIZE=-5,
            )
        self.assertIn("DB_POOL_SIZE", str(ctx.exception))

        # Non-integer string is invalid
        with self.assertRaises(ValidationError) as ctx:
            Settings(
                _env_file=None,
                DATABASE_URL="postgresql://user:pass@localhost:5432/testdb",
                DB_POOL_SIZE="not_a_number",
            )
        self.assertIn("DB_POOL_SIZE", str(ctx.exception))

    def test_max_overflow_validation(self):
        # Negative overflow is invalid
        with self.assertRaises(ValidationError) as ctx:
            Settings(
                _env_file=None,
                DATABASE_URL="postgresql://user:pass@localhost:5432/testdb",
                DB_MAX_OVERFLOW=-1,
            )
        self.assertIn("DB_MAX_OVERFLOW", str(ctx.exception))

        # 0 overflow is valid (disables overflow)
        s = Settings(
            _env_file=None,
            DATABASE_URL="postgresql://user:pass@localhost:5432/testdb",
            DB_MAX_OVERFLOW=0,
        )
        self.assertEqual(s.DB_MAX_OVERFLOW, 0)

    def test_pool_timeout_and_recycle_validation(self):
        # Timeout below 1s is invalid
        with self.assertRaises(ValidationError) as ctx:
            Settings(
                _env_file=None,
                DATABASE_URL="postgresql://user:pass@localhost:5432/testdb",
                DB_POOL_TIMEOUT=0,
            )
        self.assertIn("DB_POOL_TIMEOUT", str(ctx.exception))

        # Recycle below 60s is invalid
        with self.assertRaises(ValidationError) as ctx:
            Settings(
                _env_file=None,
                DATABASE_URL="postgresql://user:pass@localhost:5432/testdb",
                DB_POOL_RECYCLE=30,
            )
        self.assertIn("DB_POOL_RECYCLE", str(ctx.exception))

    # 2. Hardened Engine Creation
    def test_engine_creation_postgresql_pool_configuration(self):
        s = Settings(
            _env_file=None,
            DATABASE_URL="postgresql://user:pass@localhost:5432/testdb",
            DB_POOL_SIZE=7,
            DB_MAX_OVERFLOW=12,
            DB_POOL_TIMEOUT=25,
            DB_POOL_RECYCLE=2400,
        )
        custom_engine = create_app_engine(app_settings=s)
        self.assertIsInstance(custom_engine.pool, QueuePool)
        self.assertTrue(custom_engine.pool._pre_ping)
        self.assertEqual(custom_engine.pool.size(), 7)
        self.assertEqual(custom_engine.pool._max_overflow, 12)
        self.assertEqual(custom_engine.pool._timeout, 25)
        self.assertEqual(custom_engine.pool._recycle, 2400)
        self.assertFalse(custom_engine.echo)

    def test_engine_creation_sqlite_memory_static_pool(self):
        # In-memory SQLite must use StaticPool and check_same_thread=False
        sqlite_engine = create_app_engine("sqlite:///:memory:")
        self.assertIsInstance(sqlite_engine.pool, StaticPool)
        self.assertFalse(sqlite_engine.echo)

    def test_engine_creation_sqlite_file(self):
        # File-based SQLite should accept connect_args without PostgreSQL pool error
        sqlite_file_engine = create_app_engine("sqlite:///test_file.db")
        self.assertFalse(sqlite_file_engine.echo)

    # 3. Session Lifecycle & Rollback on Error
    def test_get_db_session_isolation(self):
        # Consecutive get_db generator sessions should be independent
        gen1 = get_db()
        sess1 = next(gen1)
        gen2 = get_db()
        sess2 = next(gen2)
        self.assertIsNot(sess1, sess2)
        try:
            next(gen1)
        except StopIteration:
            pass
        try:
            next(gen2)
        except StopIteration:
            pass

    def test_get_db_rollback_on_unhandled_exception(self):
        mock_session = MagicMock()
        mock_factory = MagicMock(return_value=mock_session)

        with patch("database.SessionLocal", mock_factory):
            gen = get_db()
            session = next(gen)
            self.assertEqual(session, mock_session)

            # Simulate unhandled endpoint exception
            with self.assertRaises(RuntimeError):
                try:
                    raise RuntimeError("Simulated unhandled route failure")
                except RuntimeError as exc:
                    gen.throw(exc)

            # Verify rollback and close were both called
            mock_session.rollback.assert_called_once()
            mock_session.close.assert_called_once()

    def test_get_db_closes_session_on_normal_completion(self):
        mock_session = MagicMock()
        mock_factory = MagicMock(return_value=mock_session)

        with patch("database.SessionLocal", mock_factory):
            gen = get_db()
            session = next(gen)
            self.assertEqual(session, mock_session)

            # Normal completion
            try:
                next(gen)
            except StopIteration:
                pass

            # Rollback must NOT be called on normal completion; close MUST be called
            mock_session.rollback.assert_not_called()
            mock_session.close.assert_called_once()

    # 4. Safe Database URL Masking & Health Check
    def test_mask_database_url_redacts_credentials(self):
        url = "postgresql://postgres_admin:SuperSecretPass123!@db.internal.cloud:5432/carbon_db"
        masked = mask_database_url(url)
        self.assertNotIn("SuperSecretPass123!", masked)
        self.assertNotIn("postgres_admin", masked)
        self.assertEqual(masked, "postgresql://***:***@db.internal.cloud:5432/carbon_db")

    def test_mask_database_url_handles_sqlite(self):
        self.assertEqual(mask_database_url("sqlite:///:memory:"), "sqlite:///:memory:")
        self.assertEqual(mask_database_url("sqlite:///data.db"), "sqlite:///data.db")

    def test_check_database_health_live_engine(self):
        sqlite_engine = create_app_engine("sqlite:///:memory:")
        health = check_database_health(sqlite_engine)
        self.assertEqual(health["status"], "healthy")
        self.assertNotIn("password", str(health).lower())
        self.assertEqual(health["database"], "sqlite:///:memory:")

    def test_check_database_health_failure_masks_url(self):
        failing_engine = MagicMock()
        failing_engine.url = "postgresql://secret_user:super_secret_pw@localhost:5432/db"
        failing_engine.connect.side_effect = ConnectionRefusedError("Connection refused")

        health = check_database_health(failing_engine)
        self.assertEqual(health["status"], "unhealthy")
        self.assertNotIn("super_secret_pw", str(health))
        self.assertIn("***:***@localhost:5432/db", health["database"])


if __name__ == "__main__":
    unittest.main()
