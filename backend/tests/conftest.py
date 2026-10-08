"""Unit tests must not need a running database or external services."""
import os

os.environ.setdefault("APP_MODE", "local")
os.environ.setdefault("DATABASE_URL", "postgresql://unit:unit@127.0.0.1:55432/unit")
os.environ.setdefault("JWT_SECRET", "unit-test-only-secret")
