"""Celery adapter for production with an import-free local decorator."""
from app.config import settings


if settings.app_mode == "production":
    try:
        from workers.celery_app import celery
    except ImportError as exc:
        raise RuntimeError(
            "Install backend/requirements-production.txt for production task queues"
        ) from exc
else:
    class _LocalTaskAdapter:
        @staticmethod
        def task(*args, **kwargs):
            def decorate(fn):
                return fn
            if args and callable(args[0]) and len(args) == 1 and not kwargs:
                return args[0]
            return decorate

    celery = _LocalTaskAdapter()
