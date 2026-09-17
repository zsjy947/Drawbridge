"""structlog configuration: structured JSON logs for Gateway and Runner.

Runtime logs and audit events share a common field style so that
`request_id`, `job_id`, `release_id`, `app` and `environment` can be
correlated in one place.  Machine-readable JSON goes to stdout; journald
picks it up from the systemd units.
"""

from __future__ import annotations

import logging
import sys

import structlog

_SHARED_PROCESSORS: list[structlog.typing.Processor] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.processors.TimeStamper(fmt="iso", utc=True),
    structlog.processors.StackInfoRenderer(),
]


def configure_logging(*, dev_mode: bool = False) -> None:
    """Configure structlog for the current process.

    ``dev_mode`` renders human-readable colored lines instead of JSON; it is
    meant for local development only, never for the 910B deployment.
    """
    if dev_mode:
        renderer: structlog.typing.Processor = structlog.dev.ConsoleRenderer()
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[
            *_SHARED_PROCESSORS,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_SHARED_PROCESSORS,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def get_logger(name: str, **initial_context: object) -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name).bind(**initial_context)
