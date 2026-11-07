"""Custom Logger Using Loguru, inspired by Riki-1mg gist custom_logging.py"""

import logging
import re
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


# Push device tokens are the only proof of owning a subscription, and the log
# file is kept for a year. They reach log lines through URLs: our own DELETE
# /push/subscriptions/{token} in uvicorn's access line, and the APNs request
# path /3/device/{token} in httpx2's "HTTP Request:" line and in its error
# messages. FCM tokens travel in the request body, which nothing logs. One
# patcher on every record covers all of them, whichever logger wrote it.
_PUSH_TOKEN_IN_URL = re.compile(r"(/push/subscriptions/|/3/device/)[^\s\"'?#]+")


def mask_push_tokens(message: str) -> str:
    """Replace device tokens inside push URLs with ``<token>``."""
    return _PUSH_TOKEN_IN_URL.sub(r"\1<token>", message)


def _mask_record(record: Record) -> None:
    record["message"] = mask_push_tokens(record["message"])


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
        # Lines carry battletags and request paths: two weeks is enough to
        # debug with, and is what the privacy policy can state.
        retention="14 days",
        compression="gz",
        enqueue=True,
        backtrace=True,
        level=settings.log_level.upper(),
        format=log_format,
    )
    logging.basicConfig(handlers=[InterceptHandler()], level=0)
    logging.getLogger("uvicorn.access").handlers = [InterceptHandler()]
    logging.getLogger("uvicorn.access").addFilter(MaskPushTokens())
    logging.getLogger("httpx2").addFilter(MaskApnsTokens())
    for _log in ("uvicorn", "uvicorn.error", "fastapi"):
        logging.getLogger(_log).handlers = [InterceptHandler()]
    return loguru_logger.bind(method=None)


logger = _setup_logger()
