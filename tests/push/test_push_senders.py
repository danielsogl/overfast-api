"""Tests for the APNs and FCM senders.

Both sign a real JWT against a throwaway key generated per session, so the
signing path is exercised rather than mocked; only the network is faked.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx2
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from app.adapters.push.senders import ApnsSender, FcmSender, PushSender
from app.domain.ports.push_sender import PushMessage

if TYPE_CHECKING:
    from pathlib import Path

IOS = PushMessage("apns-token-aaaaaaaa", "ios", "Rank Update", "X is now Diamond 2")
ANDROID = PushMessage("fcm-token-bbbbbbbb", "android", "Rank Update", "X is now Gold 1")


@pytest.fixture(scope="session")
def apns_key_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    key = ec.generate_private_key(ec.SECP256R1())
    path = tmp_path_factory.mktemp("push") / "apns.p8"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return path


@pytest.fixture(scope="session")
def service_account_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path_factory.mktemp("push") / "service-account.json"
    path.write_text(
        json.dumps(
            {
                "client_email": "sender@example.iam.gserviceaccount.com",
                "token_uri": "https://oauth2.googleapis.com/token",
                "private_key": key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                ).decode(),
            }
        )
    )
    return path


def _transport(handler) -> httpx2.MockTransport:
    return httpx2.MockTransport(handler)


def _patch_client(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx2.Request]:
    """Route every AsyncClient through a mock transport, recording requests."""
    seen: list[httpx2.Request] = []

    def recording(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return handler(request)

    original = httpx2.AsyncClient.__init__

    def patched(self, *args, **kwargs):
        kwargs["transport"] = _transport(recording)
        kwargs.pop("http2", None)
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx2.AsyncClient, "__init__", patched)
    return seen


class TestApnsSender:
    @pytest.mark.asyncio
    async def test_sends_the_alert_and_keeps_the_token(
        self, apns_key_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        seen = _patch_client(
            monkeypatch, lambda _: httpx2.Response(httpx2.codes.OK, json={})
        )

        gone = await ApnsSender(str(apns_key_path), "ABC1234567").send([IOS])

        assert gone == []
        request = seen[0]
        assert request.url.path.endswith(f"/3/device/{IOS.token}")
        assert request.headers["apns-topic"] == "com.mytech.OverwatchStats"
        assert request.headers["authorization"].startswith("bearer ey")
        alert = json.loads(request.content)["aps"]["alert"]
        assert alert == {"title": IOS.title, "body": IOS.body}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reason", ["Unregistered", "BadDeviceToken"])
    async def test_reports_a_dead_token(
        self, apns_key_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str
    ):
        _patch_client(
            monkeypatch,
            lambda _: httpx2.Response(httpx2.codes.GONE, json={"reason": reason}),
        )

        gone = await ApnsSender(str(apns_key_path), "ABC1234567").send([IOS])

        assert gone == [IOS.token]

    @pytest.mark.asyncio
    async def test_keeps_the_token_on_a_transient_failure(
        self, apns_key_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A 503 says nothing about the device, so the row must survive."""
        _patch_client(
            monkeypatch,
            lambda _: httpx2.Response(
                httpx2.codes.SERVICE_UNAVAILABLE, json={"reason": "ServiceUnavailable"}
            ),
        )

        gone = await ApnsSender(str(apns_key_path), "ABC1234567").send([IOS])

        assert gone == []

    @pytest.mark.asyncio
    async def test_signs_the_provider_token_once_for_many_devices(
        self, apns_key_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        seen = _patch_client(
            monkeypatch, lambda _: httpx2.Response(httpx2.codes.OK, json={})
        )
        messages = [PushMessage(f"token-{i:08d}", "ios", "T", "B") for i in range(3)]

        await ApnsSender(str(apns_key_path), "ABC1234567").send(messages)

        assert len({r.headers["authorization"] for r in seen}) == 1


class TestFcmSender:
    @staticmethod
    def _handler(send_response: httpx2.Response):
        def handler(request: httpx2.Request) -> httpx2.Response:
            if "oauth2" in str(request.url):
                return httpx2.Response(
                    httpx2.codes.OK, json={"access_token": "ya29.test"}
                )
            return send_response

        return handler

    @pytest.mark.asyncio
    async def test_exchanges_the_assertion_then_sends(
        self, service_account_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        seen = _patch_client(
            monkeypatch, self._handler(httpx2.Response(httpx2.codes.OK, json={}))
        )

        gone = await FcmSender(str(service_account_path), "proj").send([ANDROID])

        assert gone == []
        token_request, send_request = seen
        assert "oauth2" in str(token_request.url)
        assert send_request.headers["authorization"] == "Bearer ya29.test"
        assert json.loads(send_request.content)["message"]["token"] == ANDROID.token

    @pytest.mark.asyncio
    async def test_reports_an_unregistered_token(
        self, service_account_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _patch_client(
            monkeypatch,
            self._handler(
                httpx2.Response(
                    httpx2.codes.NOT_FOUND, json={"error": {"status": "UNREGISTERED"}}
                )
            ),
        )

        gone = await FcmSender(str(service_account_path), "proj").send([ANDROID])

        assert gone == [ANDROID.token]

    @pytest.mark.asyncio
    async def test_sends_nothing_when_the_token_exchange_fails(
        self, service_account_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        def handler(request: httpx2.Request) -> httpx2.Response:
            if "oauth2" in str(request.url):
                return httpx2.Response(httpx2.codes.UNAUTHORIZED, text="nope")
            pytest.fail("must not send without a token")

        _patch_client(monkeypatch, handler)

        gone = await FcmSender(str(service_account_path), "proj").send([ANDROID])

        assert gone == []


class TestPushSender:
    @pytest.mark.asyncio
    async def test_routes_each_message_to_its_own_service(self):
        class Fake:
            def __init__(self) -> None:
                self.seen: list[PushMessage] = []

            async def send(self, messages) -> list[str]:
                self.seen.extend(messages)
                return []

        apns, fcm = Fake(), Fake()

        await PushSender(apns, fcm).send([IOS, ANDROID])

        assert apns.seen == [IOS]
        assert fcm.seen == [ANDROID]

    @pytest.mark.asyncio
    async def test_delivers_android_while_apple_is_unconfigured(self):
        """Half-configured is normal while one key is still being provisioned."""

        class Fake:
            async def send(self, messages) -> list[str]:
                return [m.token for m in messages]

        gone = await PushSender(None, Fake()).send([IOS, ANDROID])

        assert gone == [ANDROID.token]

    @pytest.mark.asyncio
    async def test_ignores_a_message_for_an_unknown_platform(self):
        gone = await PushSender(None, None).send(
            [PushMessage("t-12345678", "windows", "T", "B")]
        )

        assert gone == []
