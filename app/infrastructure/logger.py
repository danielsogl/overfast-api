"""Custom Logger Using Loguru, inspired by Riki-1mg gist custom_logging.py"""

import logging
import sys
from pathlib import Path

from loguru import logger as loguru_logger

from app.config import settings


class InterceptHandler(logging.Handler):
    """InterceptionHandler class used to intercept python logs in order
    to transform them into loguru logs.
    """

    def emit(self, record):  # pragma: no cover
        try:
            level = logger.level(record.levelname).name
        except AttributeError:
            level = record.levelno

        frame, depth = logging.currentframe(), 2
        while frame is not None and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level,
            record.getMessage(),
        )


PUSH_SUBSCRIPTION_PATH = "/push/subscriptions/"


class MaskPushTokens(logging.Filter):
    """Drop the device token from uvicorn's access line for DELETE
    /push/subscriptions/{token}: the token is the only proof of owning a
    subscription, and access.log is kept for a year."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        # uvicorn's access line: (client, method, path, http_version, status)
        if (
            isinstance(args, tuple)
            and len(args) == 5  # noqa: PLR2004
            and isinstance(args[2], str)
            and args[2].startswith(PUSH_SUBSCRIPTION_PATH)
        ):
            record.args = (*args[:2], f"{PUSH_SUBSCRIPTION_PATH}<token>", *args[3:])
        return True


def _setup_logger():
    log_format = (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan> - <level>{message}</level>"
    )
    loguru_logger.remove()
    loguru_logger.add(
        sys.stdout,
        enqueue=True,
        backtrace=True,
        level=settings.log_level.upper(),
        format=log_format,
    )
    loguru_logger.add(
        str(Path(settings.logs_root_path) / "access.log"),
        rotation="1 day",
        retention="1 year",
        compression="gz",
        enqueue=True,
        backtrace=True,
        level=settings.log_level.upper(),
        format=log_format,
    )
    logging.basicConfig(handlers=[InterceptHandler()], level=0)
    logging.getLogger("uvicorn.access").handlers = [InterceptHandler()]
    logging.getLogger("uvicorn.access").addFilter(MaskPushTokens())
    for _log in ("uvicorn", "uvicorn.error", "fastapi"):
        logging.getLogger(_log).handlers = [InterceptHandler()]
    return loguru_logger.bind(method=None)


logger = _setup_logger()
