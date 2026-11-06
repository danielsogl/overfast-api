import logging

import pytest

from app.infrastructure.logger import MaskPushTokens


def _access_record(path: str) -> logging.LogRecord:
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "DELETE", path, "1.1", 204),
        None,
    )


def test_push_token_is_masked_in_the_access_line():
    record = _access_record("/push/subscriptions/0123abcd")

    assert MaskPushTokens().filter(record)
    assert "0123abcd" not in record.getMessage()
    assert "/push/subscriptions/<token>" in record.getMessage()


@pytest.mark.parametrize("path", ["/heroes", "/push/subscriptions"])
def test_other_paths_are_left_alone(path: str):
    record = _access_record(path)

    assert MaskPushTokens().filter(record)
    assert path in record.getMessage()
