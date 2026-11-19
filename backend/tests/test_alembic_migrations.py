import contextlib
import io
import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from alembic.config import Config
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

# Ensure backend directory is in path
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from config import get_settings
from database import Base, engine, init_db as db_init_db
import models


class TestAlembicMigrations(unittest.TestCase):
    """
    Test suite for Alembic configuration, metadata integration,
    import-time DDL elimination, Translation model decoupling,
    and baseline migration safety (Milestones 1.6D and 1.6D-R).
    """

    @classmethod
    def setUpClass(cls):
        cls.ini_path = os.path.join(_BACKEND_DIR, "alembic.ini")
        cls.alembic_cfg = Config(cls.ini_path)

    def test_1_alembic_loads_project_settings(self):
        """1. Verify Alembic configuration loads centralized settings."""
        settings = get_settings()
        self.assertIsNotNone(settings.DATABASE_URL)
        self.assertTrue(os.path.exists(self.ini_path))

    def test_2_alembic_uses_safe_pure_model_metadata(self):
        """2. Verify Alembic env.py targets Base.metadata with pure models and no router imports."""
        env_py_path = os.path.join(_BACKEND_DIR, "alembic", "env.py")
        with open(env_py_path, "r", encoding="utf-8") as f:
            env_code = f.read()

        self.assertIn("target_metadata = Base.metadata", env_code)
        self.assertIn("import models", env_code)
        # Ensure env.py does NOT import routers or main
        self.assertNotIn("import routers", env_code)
        self.assertNotIn("from routers", env_code)
        self.assertNotIn("import main", env_code)
        self.assertNotIn("from main", env_code)
        self.assertNotIn("create_all", env_code)

    def test_3_all_13_models_registered_in_base_metadata(self):
        """3. Verify all 13 intended application tables are registered in Base.metadata."""
        expected_tables = {
            "users",
            "cities",
            "translations",
            "air_quality_data",
            "carbon_footprint",
            "chatbot_logs",
            "feedback",
            "city_comparison",
            "aqi_history",
            "city_alerts",
            "ml_predictions",
            "mitigation_strategies",
            "alert_notifications",
        }
        actual_tables = set(Base.metadata.tables.keys())
        self.assertEqual(expected_tables, actual_tables)
        self.assertEqual(len(actual_tables), 13)

    def test_4_model_import_causes_no_import_time_ddl(self):
        """4. Verify importing models or metadata does not execute Base.metadata.create_all."""
        with open(os.path.join(_BACKEND_DIR, "models.py"), "r", encoding="utf-8") as f:
            models_code = f.read()

        # models.py must not contain top-level module create_all calls
        self.assertNotIn("Base.metadata.create_all(bind=engine)", models_code)
        self.assertIn("def init_db(", models_code)

        # Fresh isolated process import must never invoke create_all
        import subprocess
        code = (
            "import sys\n"
            "sys.path.insert(0, 'backend')\n"
            "from unittest.mock import patch\n"
            "with patch('sqlalchemy.sql.schema.MetaData.create_all') as mock_ca:\n"
            "    import models\n"
            "    assert not mock_ca.called, 'create_all called during import!'\n"
            "print('ISOLATED_IMPORT_SAFE')\n"
        )
        res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Subprocess failed: {res.stderr}")
        self.assertIn("ISOLATED_IMPORT_SAFE", res.stdout)


    def test_5_translation_model_decoupled_from_router(self):
        """5. Verify Translation model is declared in models.py and imported by the router."""
        # 1. Translation exists in models module
        self.assertTrue(hasattr(models, "Translation"))
        trans_cls = getattr(models, "Translation")
        self.assertEqual(trans_cls.__tablename__, "translations")

        # 2. Check translation router imports from models
        with open(os.path.join(_BACKEND_DIR, "routers", "translation.py"), "r", encoding="utf-8") as f:
            router_code = f.read()

        self.assertIn("from models import", router_code)
        self.assertIn("Translation", router_code)
        self.assertNotIn("class Translation(Base):", router_code)

    def test_6_offline_migration_sql_generation_without_connection(self):
        """6. Verify offline SQL generation succeeds without database connection."""
        buffer = io.StringIO()
        cfg = Config(self.ini_path)
        with contextlib.redirect_stdout(buffer):
            command.upgrade(cfg, "head", sql=True)
        sql_output = buffer.getvalue()

        self.assertIn("CREATE TABLE users", sql_output)
        self.assertIn("CREATE TABLE cities", sql_output)
        self.assertIn("CREATE TABLE air_quality_data", sql_output)
        self.assertIn("CREATE TABLE aqi_history", sql_output)
        self.assertIn("CREATE TABLE translations", sql_output)
        self.assertIn("INSERT INTO alembic_version", sql_output)

    def test_7_fresh_isolated_sqlite_database_migration(self):
        """7. Verify a genuinely empty database receives all 13 tables through Alembic."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
            temp_db = os.path.join(tmp_dir, "fresh_isolated.db")
            db_url = f"sqlite:///{temp_db}"

            # Step A: Verify database is genuinely empty initially
            test_engine = create_engine(db_url)
            try:
                inspector = inspect(test_engine)
                self.assertEqual(len(inspector.get_table_names()), 0)
            finally:
                test_engine.dispose()

            # Step B: Run upgrade head
            cfg = Config(self.ini_path)
            cfg.set_main_option("sqlalchemy.url", db_url)
            command.upgrade(cfg, "head")

            # Step C: Verify all 13 tables + alembic_version were created
            test_engine = create_engine(db_url)
            try:
                inspector = inspect(test_engine)
                created_tables = inspector.get_table_names()
                self.assertIn("alembic_version", created_tables)
                self.assertIn("users", created_tables)
                self.assertIn("air_quality_data", created_tables)
                self.assertIn("translations", created_tables)
                self.assertEqual(len(created_tables), 14)  # 13 model tables + 1 alembic_version
            finally:
                test_engine.dispose()

    def test_8_migration_downgrade_behavior(self):
        """8. Verify migration downgrade cleanly removes created application tables."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
            temp_db = os.path.join(tmp_dir, "downgrade_test.db")
            db_url = f"sqlite:///{temp_db}"

            cfg = Config(self.ini_path)
            cfg.set_main_option("sqlalchemy.url", db_url)
            command.upgrade(cfg, "head")

            test_engine = create_engine(db_url)
            try:
                command.downgrade(cfg, "base")
                inspector = inspect(test_engine)
                remaining = inspector.get_table_names()
                # All 13 application tables must be dropped; alembic_version may remain
                for table in Base.metadata.tables.keys():
                    self.assertNotIn(table, remaining)
            finally:
                test_engine.dispose()

    def test_9_explicit_application_startup_initialization(self):
        """9. Verify explicit init_db callable exists and is invoked from FastAPI startup event."""
        # 1. init_db callable exists in database and models
        self.assertTrue(callable(db_init_db))
        self.assertTrue(callable(getattr(models, "init_db", None)))

        # 2. main.py invokes init_db() in startup_event
        with open(os.path.join(_BACKEND_DIR, "main.py"), "r", encoding="utf-8") as f:
            main_code = f.read()

        self.assertIn("from database import engine, Base, init_db", main_code)
        self.assertIn("init_db()", main_code)

    def test_10_initial_migration_contains_no_accidental_drop_in_upgrade(self):
        """10. Verify initial migration contains no accidental DROP statements in upgrade()."""
        script = ScriptDirectory.from_config(self.alembic_cfg)
        rev = script.get_revision("8f726b74f22d")
        with open(rev.path, "r", encoding="utf-8") as f:
            content = f.read()

        upgrade_fn_content = content.split("def upgrade()")[1].split("def downgrade()")[0]
        self.assertNotIn("op.drop_table", upgrade_fn_content)
        self.assertNotIn("op.drop_column", upgrade_fn_content)

    def test_11_database_credentials_not_in_generated_sql_or_logs(self):
        """11. Verify database credentials do not appear in generated SQL or logs."""
        buffer = io.StringIO()
        cfg = Config(self.ini_path)
        with contextlib.redirect_stdout(buffer):
            command.upgrade(cfg, "head", sql=True)
        sql = buffer.getvalue()
        self.assertNotIn("super_secret", sql.lower())
        self.assertNotIn("mypassword", sql.lower())


if __name__ == "__main__":
    unittest.main()
