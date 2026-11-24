"""JSON logging to stdout for the dashboard and the agent.

One JSON object per line so `kubectl logs` stays readable and log tooling can
filter by field. Callers pass structured context with `extra={...}`; those
keys become top-level fields. Never pass header values, tokens or keys.
"""
from __future__ import annotations

import json
import logging
import sys
import time

# Attributes every LogRecord has; anything else came in through `extra`.
_STANDARD = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def __init__(self, component: str) -> None:
        super().__init__()
        self.component = component

    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
                  + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "component": self.component,
            "event": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD and not key.startswith("_"):
                out[key] = value
        if record.exc_info:
            out["error"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def setup(component: str, level: int = logging.INFO) -> logging.Logger:
    """Route the root logger to stdout as JSON and return the component logger.
    Safe to call more than once (replaces the handler instead of adding one)."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(component))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    return logging.getLogger(component)
