import json
import logging
from contextvars import ContextVar
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

request_context: ContextVar[dict | None] = ContextVar("request_context", default=None)


class JsonFormatter(logging.Formatter):
    def format(self, record):
        data = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),
            **(request_context.get() or {}),
            **getattr(record, "fields", {}),
        }
        return json.dumps(data, ensure_ascii=False)


def setup_logging(level="INFO"):
    logger = logging.getLogger("qq_agent")
    logger.setLevel(level)
    logger.propagate = False
    for handler in logger.handlers:
        handler.close()
    logger.handlers.clear()
    Path("logs").mkdir(exist_ok=True)
    for handler in (
        logging.StreamHandler(),
        RotatingFileHandler("logs/agent.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"),
    ):
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    # SDK HTTP diagnostics can contain request details; never enable them from app log level.
    for name in ("openai", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
