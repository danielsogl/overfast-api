import httpx2
import pytest

from app.infrastructure.logger import logger, mask_push_tokens


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            '1.2.3.4:5 - "DELETE /push/subscriptions/0123abcd HTTP/1.1" 204',
            '1.2.3.4:5 - "DELETE /push/subscriptions/<token> HTTP/1.1" 204',
        ),
        (
            'HTTP Request: POST https://api.push.apple.com/3/device/0123abcd "HTTP/2 200 OK"',
            'HTTP Request: POST https://api.push.apple.com/3/device/<token> "HTTP/2 200 OK"',
        ),
        (
            "[push] APNs request failed: https://api.push.apple.com/3/device/0123abcd?x=1",
            "[push] APNs request failed: https://api.push.apple.com/3/device/<token>?x=1",
        ),
        ('"GET /heroes HTTP/1.1" 200', '"GET /heroes HTTP/1.1" 200'),
        (
            '"POST /push/subscriptions HTTP/1.1" 201',
            '"POST /push/subscriptions HTTP/1.1" 201',
        ),
    ],
)
def test_mask_push_tokens(message: str, expected: str):
    assert mask_push_tokens(message) == expected


def test_httpx_request_line_reaches_the_sinks_masked():
    lines: list[str] = []
    sink = logger.add(lines.append, format="{message}", level="INFO")
    device = "7147377abf95bda7ca67788add1a4651"
    try:
        with httpx2.Client(
            transport=httpx2.MockTransport(lambda _: httpx2.Response(200))
        ) as client:
            client.post(f"https://api.push.apple.com/3/device/{device}")
    finally:
        logger.remove(sink)

    assert any("/3/device/<token>" in line for line in lines)
    assert not any(device in line for line in lines)
