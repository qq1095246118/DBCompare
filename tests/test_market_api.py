import asyncio
import importlib
import base64
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading

import pytest

from getMarket.bubblemaps.tool.market_identity import make_target
from getMarket.bubblemaps.tool.market_artifacts import write_raw_response


TOKEN = "0x1111111111111111111111111111111111111111"
MEMBER = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
MAP_URL = "https://v2.example.test/map"
BUNDLE_URL = "https://v2.example.test/assets/index-test.js"


class FakeResponse:
    def __init__(self, status: int, payload: object) -> None:
        self.status = status
        self.body = (
            payload
            if isinstance(payload, bytes)
            else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        )


class FakeTransport:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[object] = []

    async def send(self, request, *, timeout: float):
        assert timeout == 2.0
        self.calls.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _frontend(secret: str, base_url: str = "https://api.example.test") -> list[object]:
    return [
        FakeResponse(200, b'<script type="module" src="/assets/index-test.js"></script>'),
        FakeResponse(
            200,
            (
                'const env={VITE_API_BASE_URL:"'
                + base_url
                + '",VITE_API_VALIDATION_SECRET:"'
                + secret
                + '"};'
            ).encode(),
        ),
    ]


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _decode_and_verify_jwt(token: str, secret: str) -> tuple[dict, dict]:
    header, payload, signature = token.split(".")
    expected = hmac.new(
        secret.encode("utf-8"),
        f"{header}.{payload}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    assert hmac.compare_digest(_b64decode(signature), expected)
    return (
        json.loads(_b64decode(header)),
        json.loads(_b64decode(payload)),
    )

def test_market_api_module_exists() -> None:
    module = (
        Path(__file__).parents[1]
        / "getMarket/bubblemaps/tool/bubblemaps_api.py"
    )

    assert module.is_file()


def test_market_api_exports_client_type() -> None:
    api = importlib.import_module("getMarket.bubblemaps.tool.bubblemaps_api")

    assert getattr(api, "BubblemapsApiClient", None) is not None


@pytest.mark.parametrize("field", ["timeout", "retry_delay", "min_request_interval"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_client_rejects_nonfinite_retry_timing(field, value) -> None:
    api = importlib.import_module("getMarket.bubblemaps.tool.bubblemaps_api")
    arguments = {"timeout": 2.0, "retry_delay": 0.0, field: value}

    with pytest.raises(ValueError, match="retry settings"):
        api.BubblemapsApiClient(FakeTransport([]), map_url=MAP_URL, **arguments)


@pytest.mark.parametrize("value", [-1, -0.1])
def test_client_rejects_negative_min_request_interval(value) -> None:
    api = importlib.import_module("getMarket.bubblemaps.tool.bubblemaps_api")

    with pytest.raises(ValueError, match="retry settings"):
        api.BubblemapsApiClient(
            FakeTransport([]), map_url=MAP_URL, min_request_interval=value
        )


class _FakeMonotonicClock:
    def __init__(self, value: float = 0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    async def sleep(self, delay: float) -> None:
        self.value += delay


class _TimedTransport(FakeTransport):
    def __init__(self, outcomes, clock) -> None:
        super().__init__(outcomes)
        self.clock = clock
        self.endpoint_starts: list[float] = []

    async def send(self, request, *, timeout: float):
        if request.url.startswith("https://api.example.test/"):
            self.endpoint_starts.append(self.clock())
        return await super().send(request, timeout=timeout)


class _FakeDualClock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def wall_time(self) -> float:
        return self.value

    async def sleep(self, delay: float) -> None:
        self.value += delay


class _JwtTimingTransport(FakeTransport):
    def __init__(self, outcomes, clock, secret: str) -> None:
        super().__init__(outcomes)
        self.clock = clock
        self.secret = secret
        self.jwt_at_send: list[tuple[str, int, float]] = []

    async def send(self, request, *, timeout: float):
        if request.url.startswith("https://api.example.test/"):
            _header, claims = _decode_and_verify_jwt(
                request.headers["x-validation"], self.secret
            )
            self.jwt_at_send.append(
                (request.headers["x-validation"], claims["exp"], self.clock.wall_time())
            )
        return await super().send(request, timeout=timeout)


@pytest.mark.asyncio
async def test_concurrent_rate_limited_endpoint_requests_sign_after_their_slot(
    monkeypatch,
) -> None:
    api = importlib.import_module("getMarket.bubblemaps.tool.bubblemaps_api")
    monkeypatch.setattr(api, "_JWT_LIFETIME_SECONDS", 1)
    clock = _FakeDualClock()
    secret = "jwt-timing-secret"
    transport = _JwtTimingTransport(
        _frontend(secret) + [FakeResponse(200, []), FakeResponse(200, [])],
        clock,
        secret,
    )
    client = api.BubblemapsApiClient(
        transport,
        map_url=MAP_URL,
        timeout=2.0,
        retry_delay=0,
        min_request_interval=2.1,
        clock=clock.wall_time,
        monotonic_clock=clock.monotonic,
        sleep=clock.sleep,
    )
    target = make_target("bsc", TOKEN)

    await asyncio.gather(client.top_holders(target), client.top_holders(target))

    assert [expiration > sent_at for _jwt, expiration, sent_at in transport.jwt_at_send] == [True, True]
    assert transport.jwt_at_send[0][0] != transport.jwt_at_send[1][0]


@pytest.mark.asyncio
async def test_rate_limited_retry_signs_a_fresh_token_after_its_slot(monkeypatch) -> None:
    api = importlib.import_module("getMarket.bubblemaps.tool.bubblemaps_api")
    monkeypatch.setattr(api, "_JWT_LIFETIME_SECONDS", 1)
    clock = _FakeDualClock()
    secret = "retry-jwt-timing-secret"
    transport = _JwtTimingTransport(
        _frontend(secret) + [FakeResponse(503, {}), FakeResponse(200, [])],
        clock,
        secret,
    )
    client = api.BubblemapsApiClient(
        transport,
        map_url=MAP_URL,
        timeout=2.0,
        retry_delay=0.1,
        min_request_interval=2.1,
        clock=clock.wall_time,
        monotonic_clock=clock.monotonic,
        sleep=clock.sleep,
    )

    result = await client.top_holders(make_target("bsc", TOKEN))

    assert result.metadata.attempts == 2
    assert [expiration > sent_at for _jwt, expiration, sent_at in transport.jwt_at_send] == [True, True]
    assert transport.jwt_at_send[0][0] != transport.jwt_at_send[1][0]


@pytest.mark.asyncio
async def test_endpoint_requests_are_spaced_by_min_request_interval() -> None:
    api = importlib.import_module("getMarket.bubblemaps.tool.bubblemaps_api")
    clock = _FakeMonotonicClock(10)
    transport = _TimedTransport(
        _frontend("interval-secret") + [FakeResponse(200, []), FakeResponse(200, [])],
        clock,
    )
    client = api.BubblemapsApiClient(
        transport,
        map_url=MAP_URL,
        timeout=2.0,
        retry_delay=0,
        min_request_interval=2.5,
        monotonic_clock=clock,
        sleep=clock.sleep,
    )
    target = make_target("bsc", TOKEN)

    await client.top_holders(target)
    await client.subgraph(target, [MEMBER])

    assert transport.endpoint_starts == [10, 12.5]


@pytest.mark.asyncio
async def test_endpoint_retry_attempts_are_spaced_after_retry_delay() -> None:
    api = importlib.import_module("getMarket.bubblemaps.tool.bubblemaps_api")
    clock = _FakeMonotonicClock()
    transport = _TimedTransport(
        _frontend("interval-secret") + [FakeResponse(503, {}), FakeResponse(200, [])],
        clock,
    )
    client = api.BubblemapsApiClient(
        transport,
        map_url=MAP_URL,
        timeout=2.0,
        retry_delay=0.5,
        min_request_interval=3,
        monotonic_clock=clock,
        sleep=clock.sleep,
    )

    result = await client.top_holders(make_target("bsc", TOKEN))

    assert result.metadata.attempts == 2
    assert transport.endpoint_starts == [0, 3]


@pytest.mark.asyncio
async def test_concurrent_endpoint_requests_share_one_rate_limit_slot() -> None:
    api = importlib.import_module("getMarket.bubblemaps.tool.bubblemaps_api")
    clock = _FakeMonotonicClock()
    transport = _TimedTransport(
        _frontend("interval-secret") + [FakeResponse(200, []), FakeResponse(200, [])],
        clock,
    )
    client = api.BubblemapsApiClient(
        transport,
        map_url=MAP_URL,
        timeout=2.0,
        retry_delay=0,
        min_request_interval=2,
        monotonic_clock=clock,
        sleep=clock.sleep,
    )
    target = make_target("bsc", TOKEN)

    await asyncio.gather(client.top_holders(target), client.top_holders(target))

    assert transport.endpoint_starts == [0, 2]


@pytest.mark.asyncio
async def test_zero_min_request_interval_does_not_delay_endpoint_requests() -> None:
    api = importlib.import_module("getMarket.bubblemaps.tool.bubblemaps_api")
    clock = _FakeMonotonicClock()
    transport = _TimedTransport(
        _frontend("interval-secret") + [FakeResponse(200, []), FakeResponse(200, [])],
        clock,
    )
    client = api.BubblemapsApiClient(
        transport,
        map_url=MAP_URL,
        timeout=2.0,
        retry_delay=0,
        monotonic_clock=clock,
        sleep=clock.sleep,
    )
    target = make_target("bsc", TOKEN)

    await client.top_holders(target)
    await client.subgraph(target, [MEMBER])

    assert transport.endpoint_starts == [0, 0]


@pytest.mark.asyncio
async def test_client_uses_frontend_config_jwt_and_expected_endpoint_shapes() -> None:
    api = importlib.import_module("getMarket.bubblemaps.tool.bubblemaps_api")
    secret = "unit-test-signing-secret"
    transport = FakeTransport(
        _frontend(secret)
        + [
            FakeResponse(200, [{"address": MEMBER}]),
            FakeResponse(200, [{"edge": True}]),
            FakeResponse(200, [{"transfer": True}]),
        ]
    )
    client = api.BubblemapsApiClient(
        transport,
        map_url=MAP_URL,
        timeout=2.0,
        retry_delay=0,
        clock=lambda: 1_700_000_000,
    )
    target = make_target("bsc", TOKEN)

    holders = await client.top_holders(target)
    subgraph = await client.subgraph(target, [MEMBER])
    transfers = await client.transfers(target, MEMBER)

    assert holders.payload == [{"address": MEMBER}]
    assert subgraph.payload == [{"edge": True}]
    assert transfers.payload == [{"transfer": True}]
    holder, graph, transfer = transport.calls[2:]
    assert transport.calls[0].headers == {
        "accept": "text/html,application/xhtml+xml",
        "user-agent": "bubblemaps-db-compare/1.0",
    }
    assert transport.calls[1].headers == {
        "accept": "application/javascript,text/javascript,*/*;q=0.1",
        "user-agent": "bubblemaps-db-compare/1.0",
    }
    assert holder.method == "POST"
    assert holder.url == (
        "https://api.example.test/addresses/token-top-holders?count=300&nocache=false"
    )
    assert json.loads(holder.body) == {"chain": "bsc", "address": TOKEN}
    assert graph.method == "POST"
    assert graph.url == (
        "https://api.example.test/relationships/subgraph"
        f"?whitelist_token_address={TOKEN}&whitelist_token_chain=bsc"
        "&queue_whitelisted_token_map=false"
    )
    assert json.loads(graph.body) == [MEMBER]
    assert transfer.method == "GET"
    assert transfer.url == (
        "https://api.example.test/relationships/transfers"
        f"?address={MEMBER}&whitelist_token_address={TOKEN}"
        "&whitelist_token_chain=bsc"
    )
    header, claims = _decode_and_verify_jwt(holder.headers["x-validation"], secret)
    assert header == {"alg": "HS256"}
    assert claims == {
        "data": "/addresses/token-top-holders?count=300&nocache=false",
        "exp": 1_700_000_300,
    }
    assert holders.metadata.method == "POST"
    assert holders.metadata.status == 200
    assert not hasattr(holders.metadata, "headers")


@pytest.mark.asyncio
async def test_endpoint_uses_browser_user_agent_without_persisting_headers(
    tmp_path,
) -> None:
    api = importlib.import_module("getMarket.bubblemaps.tool.bubblemaps_api")
    transport = FakeTransport(
        _frontend(
            "browser-user-agent-secret",
            base_url="https://api.bubblemaps.io",
        )
        + [FakeResponse(200, [{"address": MEMBER}])]
    )
    client = api.BubblemapsApiClient(
        transport,
        map_url=MAP_URL,
        timeout=2.0,
        retry_delay=0,
    )
    target = make_target("bsc", TOKEN)

    result = await client.top_holders(target)

    endpoint_request = transport.calls[2]
    assert endpoint_request.headers["user-agent"] == (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )
    assert "origin" not in endpoint_request.headers
    assert "referer" not in endpoint_request.headers
    assert not hasattr(result.metadata, "headers")

    relative = write_raw_response(tmp_path, target, "holders", result)
    artifact = json.loads((tmp_path / relative).read_text(encoding="utf-8"))
    assert artifact["request"] == {
        "method": "POST",
        "url": (
            "https://api.bubblemaps.io/addresses/token-top-holders"
            "?count=300&nocache=false"
        ),
        "status": 200,
        "attempts": 1,
    }
    assert "headers" not in artifact
    assert "Mozilla/5.0" not in json.dumps(artifact)


@pytest.mark.asyncio
async def test_approved_get_method_names_match_short_method_behavior() -> None:
    api = importlib.import_module("getMarket.bubblemaps.tool.bubblemaps_api")
    transport = FakeTransport(
        _frontend("approved-method-secret")
        + [
            FakeResponse(200, {"holders": True}),
            FakeResponse(200, {"subgraph": True}),
            FakeResponse(200, {"transfers": True}),
        ]
    )
    client = api.BubblemapsApiClient(
        transport, map_url=MAP_URL, timeout=2.0, retry_delay=0
    )
    target = make_target("bsc", TOKEN)

    assert (await client.get_top_holders(target)).payload == {"holders": True}
    assert (await client.get_subgraph(target, [MEMBER])).payload == {"subgraph": True}
    assert (await client.get_transfers(target, MEMBER)).payload == {"transfers": True}


@pytest.mark.asyncio
async def test_401_refreshes_frontend_config_once_then_retries_request() -> None:
    api = importlib.import_module("getMarket.bubblemaps.tool.bubblemaps_api")
    first_secret = "first-signing-secret"
    second_secret = "second-signing-secret"
    transport = FakeTransport(
        _frontend(first_secret)
        + [FakeResponse(401, {"message": "expired"})]
        + _frontend(second_secret)
        + [FakeResponse(200, [])]
    )
    client = api.BubblemapsApiClient(
        transport,
        map_url=MAP_URL,
        timeout=2.0,
        retry_delay=0,
        clock=lambda: 1_700_000_000,
    )

    result = await client.top_holders(make_target("bsc", TOKEN))

    assert result.payload == []
    assert [request.url for request in transport.calls] == [
        MAP_URL,
        BUNDLE_URL,
        "https://api.example.test/addresses/token-top-holders?count=300&nocache=false",
        MAP_URL,
        BUNDLE_URL,
        "https://api.example.test/addresses/token-top-holders?count=300&nocache=false",
    ]
    _header, claims = _decode_and_verify_jwt(
        transport.calls[-1].headers["x-validation"], second_secret
    )
    assert claims["data"] == "/addresses/token-top-holders?count=300&nocache=false"


@pytest.mark.asyncio
async def test_timeout_and_5xx_retries_are_bounded_and_errors_are_redacted() -> None:
    api = importlib.import_module("getMarket.bubblemaps.tool.bubblemaps_api")
    secret = "never-expose-this-signing-secret"
    transport = FakeTransport(
        _frontend(secret)
        + [TimeoutError(secret), TimeoutError(secret), FakeResponse(200, [])]
        + [FakeResponse(503, {"message": secret})] * 3
    )
    client = api.BubblemapsApiClient(
        transport,
        map_url=MAP_URL,
        timeout=2.0,
        retry_delay=0,
        max_attempts=3,
    )
    target = make_target("bsc", TOKEN)

    successful = await client.top_holders(target)
    assert successful.payload == []
    assert successful.metadata.attempts == 3
    with pytest.raises(api.BubblemapsApiError) as raised:
        await client.subgraph(target, [MEMBER])

    endpoint_calls = [
        call
        for call in transport.calls
        if call.url.startswith("https://api.example.test/")
    ]
    assert len(endpoint_calls) == 6
    assert raised.value.attempts == 3
    assert secret not in str(raised.value)
    assert secret not in repr(raised.value)


@pytest.mark.asyncio
async def test_first_4xx_reports_one_attempt_even_when_bound_is_five() -> None:
    api = importlib.import_module("getMarket.bubblemaps.tool.bubblemaps_api")
    secret = "first-4xx-secret"
    transport = FakeTransport(
        _frontend(secret) + [FakeResponse(400, {"error": "bad"})]
    )
    client = api.BubblemapsApiClient(
        transport,
        map_url=MAP_URL,
        timeout=2.0,
        retry_delay=0,
        max_attempts=5,
    )

    with pytest.raises(api.BubblemapsApiError) as raised:
        await client.top_holders(make_target("bsc", TOKEN))

    assert type(raised.value) is api.BubblemapsApiError
    assert raised.value.attempts == 1
    assert len(transport.calls) == 3
    assert secret not in str(raised.value)
    assert secret not in repr(raised.value)


@pytest.mark.asyncio
async def test_top_holders_unavailable_raises_typed_non_retryable_error() -> None:
    api = importlib.import_module("getMarket.bubblemaps.tool.bubblemaps_api")
    secret = "top-holders-unavailable-secret"
    transport = FakeTransport(
        _frontend(secret)
        + [
            FakeResponse(
                400,
                {"detail": "Top holders not available for this token."},
            )
        ]
    )
    client = api.BubblemapsApiClient(
        transport,
        map_url=MAP_URL,
        timeout=2.0,
        retry_delay=0,
        max_attempts=5,
    )

    with pytest.raises(api.TopHoldersUnavailableError) as raised:
        await client.top_holders(make_target("bsc", TOKEN))

    assert raised.value.attempts == 1
    assert raised.value.http_status == 400
    assert len(transport.calls) == 3
    assert secret not in str(raised.value)
    assert secret not in repr(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response_body", "body_marker"),
    [
        (b"{unavailable-body-parse-failure", "unavailable-body-parse-failure"),
        (["unavailable-body-non-dict"], "unavailable-body-non-dict"),
        (
            {"detail": "Top holders not available for this token?"},
            "Top holders not available for this token?",
        ),
    ],
)
async def test_top_holders_unavailable_negative_responses_remain_generic(
    response_body,
    body_marker,
) -> None:
    api = importlib.import_module("getMarket.bubblemaps.tool.bubblemaps_api")
    secret = "top-holders-negative-classification-secret"
    transport = FakeTransport(_frontend(secret) + [FakeResponse(400, response_body)])
    client = api.BubblemapsApiClient(
        transport,
        map_url=MAP_URL,
        timeout=2.0,
        retry_delay=0,
        max_attempts=5,
    )

    with pytest.raises(api.BubblemapsApiError) as raised:
        await client.top_holders(make_target("bsc", TOKEN))

    assert type(raised.value) is api.BubblemapsApiError
    assert raised.value.attempts == 1
    assert len(transport.calls) == 3
    assert secret not in str(raised.value)
    assert secret not in repr(raised.value)
    assert body_marker not in str(raised.value)
    assert body_marker not in repr(raised.value)


@pytest.mark.asyncio
async def test_transfers_top_holders_unavailable_detail_remains_generic() -> None:
    api = importlib.import_module("getMarket.bubblemaps.tool.bubblemaps_api")
    secret = "non-holders-unavailable-secret"
    detail = "Top holders not available for this token."
    transport = FakeTransport(_frontend(secret) + [FakeResponse(400, {"detail": detail})])
    client = api.BubblemapsApiClient(
        transport,
        map_url=MAP_URL,
        timeout=2.0,
        retry_delay=0,
        max_attempts=5,
    )

    with pytest.raises(api.BubblemapsApiError) as raised:
        await client.transfers(make_target("bsc", TOKEN), MEMBER)

    assert type(raised.value) is api.BubblemapsApiError
    assert raised.value.attempts == 1
    assert len(transport.calls) == 3
    assert secret not in str(raised.value)
    assert secret not in repr(raised.value)
    assert detail not in str(raised.value)
    assert detail not in repr(raised.value)


@pytest.mark.asyncio
async def test_two_5xx_responses_then_success_reports_three_attempts() -> None:
    api = importlib.import_module("getMarket.bubblemaps.tool.bubblemaps_api")
    transport = FakeTransport(
        _frontend("retry-count-secret")
        + [FakeResponse(503, {}), FakeResponse(502, {}), FakeResponse(200, [])]
    )
    client = api.BubblemapsApiClient(
        transport,
        map_url=MAP_URL,
        timeout=2.0,
        retry_delay=0,
        max_attempts=5,
    )

    result = await client.top_holders(make_target("bsc", TOKEN))

    assert result.metadata.attempts == 3


@pytest.mark.asyncio
async def test_429_retries_with_bounded_server_retry_after_then_succeeds(
    monkeypatch,
) -> None:
    api = importlib.import_module("getMarket.bubblemaps.tool.bubblemaps_api")
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(api.asyncio, "sleep", record_sleep)
    transport = FakeTransport(
        _frontend("rate-limit-secret")
        + [
            FakeResponse(429, {"error_code": 1015, "retryable": True, "retry_after": 30}),
            FakeResponse(200, []),
        ]
    )
    client = api.BubblemapsApiClient(
        transport,
        map_url=MAP_URL,
        timeout=2.0,
        retry_delay=0.25,
        max_attempts=3,
    )

    result = await client.transfers(make_target("bsc", TOKEN), MEMBER)

    assert result.payload == []
    assert result.metadata.attempts == 2
    assert sleeps == [30]
    assert len(transport.calls) == 4


@pytest.mark.asyncio
async def test_429_exhausts_existing_attempt_bound_without_body_leak(monkeypatch) -> None:
    api = importlib.import_module("getMarket.bubblemaps.tool.bubblemaps_api")
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(api.asyncio, "sleep", record_sleep)
    secret = "rate-limit-body-must-not-leak"
    transport = FakeTransport(
        _frontend("rate-limit-secret")
        + [FakeResponse(429, {"message": secret, "retry_after": 0})] * 3
    )
    client = api.BubblemapsApiClient(
        transport,
        map_url=MAP_URL,
        timeout=2.0,
        retry_delay=0,
        max_attempts=3,
    )

    with pytest.raises(api.BubblemapsApiError) as raised:
        await client.transfers(make_target("bsc", TOKEN), MEMBER)

    assert raised.value.attempts == 3
    assert sleeps == [0, 0]
    assert len(transport.calls) == 5
    assert secret not in str(raised.value)
    assert secret not in repr(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response_body", "expected_delay"),
    [
        (b"not-json", 0.5),
        (json.dumps([{"retry_after": 20}]).encode(), 0.5),
        (json.dumps({"retry_after": True}).encode(), 0.5),
        (json.dumps({"retry_after": -1}).encode(), 0.5),
        (json.dumps({"retry_after": 999}).encode(), 300),
        (b'{"retry_after":' + b"9" * 1_000 + b"}", 300),
        (b'{"retry_after":' + b"9" * 5_000 + b"}", 0.5),
        (b"[" * 2_000 + b"0" + b"]" * 2_000, 0.5),
    ],
)
async def test_429_invalid_retry_after_falls_back_and_large_value_is_capped(
    monkeypatch,
    response_body,
    expected_delay,
) -> None:
    api = importlib.import_module("getMarket.bubblemaps.tool.bubblemaps_api")
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(api.asyncio, "sleep", record_sleep)
    transport = FakeTransport(
        _frontend("rate-limit-secret")
        + [FakeResponse(429, response_body), FakeResponse(200, [])]
    )
    client = api.BubblemapsApiClient(
        transport,
        map_url=MAP_URL,
        timeout=2.0,
        retry_delay=0.5,
        max_attempts=2,
    )

    result = await client.top_holders(make_target("bsc", TOKEN))

    assert result.metadata.attempts == 2
    assert sleeps == [expected_delay]


@pytest.mark.asyncio
async def test_second_401_fails_without_another_refresh_or_secret_leak() -> None:
    api = importlib.import_module("getMarket.bubblemaps.tool.bubblemaps_api")
    secret = "second-401-secret"
    transport = FakeTransport(
        _frontend(secret)
        + [FakeResponse(401, {"message": secret})]
        + _frontend("refreshed-secret")
        + [FakeResponse(401, {"message": secret})]
    )
    client = api.BubblemapsApiClient(
        transport,
        map_url=MAP_URL,
        timeout=2.0,
        retry_delay=0,
    )

    with pytest.raises(api.BubblemapsApiError) as raised:
        await client.top_holders(make_target("bsc", TOKEN))

    assert len(transport.calls) == 6
    assert secret not in str(raised.value)


@pytest.mark.asyncio
async def test_frontend_discovery_retries_transient_timeout_with_the_same_bound() -> None:
    api = importlib.import_module("getMarket.bubblemaps.tool.bubblemaps_api")
    transport = FakeTransport(
        [TimeoutError("untrusted transport detail")]
        + _frontend("config-retry-secret")
        + [FakeResponse(200, [])]
    )
    client = api.BubblemapsApiClient(
        transport,
        map_url=MAP_URL,
        timeout=2.0,
        retry_delay=0,
        max_attempts=3,
    )

    result = await client.top_holders(make_target("bsc", TOKEN))

    assert result.payload == []
    assert [call.url for call in transport.calls] == [
        MAP_URL,
        MAP_URL,
        BUNDLE_URL,
        "https://api.example.test/addresses/token-top-holders?count=300&nocache=false",
    ]


def test_urllib_transport_does_not_follow_redirect_or_forward_validation_header() -> None:
    api = importlib.import_module("getMarket.bubblemaps.tool.bubblemaps_api")
    observed: list[tuple[str, str | None]] = []

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            observed.append((self.path, self.headers.get("X-validation")))
            if self.path == "/start":
                self.send_response(302)
                self.send_header("Location", "/target")
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *_args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        response = api.UrllibTransport._send_sync(
            api.ApiRequest(
                "GET",
                f"http://127.0.0.1:{server.server_port}/start",
                None,
                {"X-validation": "must-not-cross-origin"},
            ),
            2.0,
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert response.status == 302
    assert observed == [("/start", "must-not-cross-origin")]


@pytest.mark.asyncio
async def test_config_invalidation_only_clears_the_config_used_by_stale_request() -> None:
    api = importlib.import_module("getMarket.bubblemaps.tool.bubblemaps_api")
    client = api.BubblemapsApiClient(
        FakeTransport([]), map_url=MAP_URL, timeout=2.0, retry_delay=0
    )
    stale = api._FrontendConfig("https://api.stale.test", "stale")
    current = api._FrontendConfig("https://api.current.test", "current")
    client._config = current

    await client._invalidate_config(stale)
    assert client._config is current

    await client._invalidate_config(current)
    assert client._config is None
