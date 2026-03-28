"""Structured JSON logging with correlation ID propagation.

Provides:
- ``CorrelationIdMiddleware`` — Starlette middleware that reads or generates a
  correlation ID and stores it in ``contextvars`` for the duration of the
  request.
- ``get_correlation_id()`` — retrieve the current correlation ID from any
  async or sync code running inside the request context.
- ``setup_logging()`` — configure Python's logging to emit structured JSON
  lines with the correlation ID injected into every record.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

# ── Context variable for correlation ID ──────────────────────────────

_correlation_id_ctx: ContextVar[str] = ContextVar("correlation_id", default="")

CORRELATION_ID_HEADER = "X-Correlation-ID"


def get_correlation_id() -> str:
    """Return the current request's correlation ID, or empty string if unset."""
    return _correlation_id_ctx.get()


# ── Starlette middleware ─────────────────────────────────────────────


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Extract or generate a correlation ID for each request.

    The ID is read from the ``X-Correlation-ID`` request header.  If the
    header is absent a new UUID4 is generated.  The ID is stored in
    ``contextvars`` so that log records automatically include it.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        correlation_id = request.headers.get(CORRELATION_ID_HEADER) or str(
            uuid.uuid4()
        )
        token = _correlation_id_ctx.set(correlation_id)
        try:
            response = await call_next(request)
            response.headers[CORRELATION_ID_HEADER] = correlation_id
            return response
        finally:
            _correlation_id_ctx.reset(token)


# ── Logging filter ───────────────────────────────────────────────────


class CorrelationIdFilter(logging.Filter):
    """Inject ``correlation_id`` into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = _correlation_id_ctx.get()  # type: ignore[attr-defined]
        return True


# ── JSON formatter ───────────────────────────────────────────────────


class JSONFormatter(logging.Formatter):
    """Emit log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": getattr(record, "correlation_id", ""),
        }

        # Collect any extra fields that were passed via `extra={}` on the
        # logging call, excluding standard LogRecord attributes.
        _STANDARD_ATTRS = {
            "name",
            "msg",
            "args",
            "created",
            "relativeCreated",
            "exc_info",
            "exc_text",
            "stack_info",
            "lineno",
            "funcName",
            "pathname",
            "filename",
            "module",
            "thread",
            "threadName",
            "process",
            "processName",
            "levelname",
            "levelno",
            "message",
            "msecs",
            "correlation_id",
            "taskName",
        }
        extra = {
            k: v for k, v in record.__dict__.items() if k not in _STANDARD_ATTRS
        }
        if extra:
            log_entry["extra"] = extra

        if record.exc_info and record.exc_info[1] is not None:
            log_entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_entry, default=str)


# ── Setup function ───────────────────────────────────────────────────


def setup_logging(
    level: str = "INFO",
    log_format: str = "json",
) -> None:
    """Configure the root logger with structured output and correlation IDs.

    Parameters
    ----------
    level:
        Logging level name (DEBUG, INFO, WARNING, ERROR, CRITICAL).
    log_format:
        ``"json"`` for structured JSON lines, ``"text"`` for human-readable
        output (useful during local development).
    """
    root = logging.getLogger()

    # Avoid adding duplicate handlers on repeated calls
    if any(
        isinstance(h, logging.StreamHandler) and getattr(h, "_slate_managed", False)
        for h in root.handlers
    ):
        return

    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    handler = logging.StreamHandler(sys.stdout)
    handler._slate_managed = True  # type: ignore[attr-defined]

    if log_format == "json":
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s (%(correlation_id)s) %(message)s",
                defaults={"correlation_id": ""},
            )
        )

    handler.addFilter(CorrelationIdFilter())
    root.addHandler(handler)

    # Suppress overly chatty third-party loggers
    for noisy in ("uvicorn.access", "sqlalchemy.engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
