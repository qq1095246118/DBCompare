import json
import socket
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

import pytest

from getMarket.Polymarket.tool.polymarket_api import (
    ApiTransportResponse,
    PolymarketApiClient,
    PolymarketApiError,
)


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def send(self, request, *, timeout):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def response(status, payload, headers=None):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return ApiTransportResponse(status, body, headers or {})


def api_market(market_id="1", *, tags=None):
    return {
        "id": market_id,
        "tags": [{"id": "tag-1", "slug": "election"}] if tags is None else tags,
    }


@pytest.mark.asyncio
async def test_collect_tag_follows_next_cursor_until_terminal_page():
    transport = FakeTransport([
        response(200, {"markets": [api_market("1")], "next_cursor": "abc"}),
        response(200, {"markets": [api_market("2")], "next_cursor": None}),
    ])
    client = PolymarketApiClient(transport=transport, retry_delay=0)

    pages = await client.collect_tag("2", page_limit=20)

    assert [page.cursor for page in pages] == [None, "abc"]
    assert [page.payload["markets"][0]["id"] for page in pages] == ["1", "2"]
    first = parse_qs(urlsplit(transport.requests[0].url).query)
    second = parse_qs(urlsplit(transport.requests[1].url).query)
    assert first == {
        "tag_id": ["2"], "active": ["true"], "closed": ["false"],
        "include_tag": ["true"], "limit": ["20"],
    }
    assert second["after_cursor"] == ["abc"]
    assert second["include_tag"] == ["true"]


@pytest.mark.asyncio
async def test_collect_tag_retries_timeout_and_503():
    transport = FakeTransport([
        TimeoutError(),
        response(503, {"error": "busy"}),
        response(200, {"markets": [], "next_cursor": None}),
    ])
    client = PolymarketApiClient(
        transport=transport, max_attempts=3, retry_delay=0,
    )

    pages = await client.collect_tag("2", page_limit=20)

    assert pages[0].attempts == 3
    assert len(transport.requests) == 3


@pytest.mark.asyncio
async def test_collect_tag_retries_urllib_wrapped_timeout():
    transport = FakeTransport([
        URLError(socket.timeout()),
        response(200, {"markets": [], "next_cursor": None}),
    ])
    client = PolymarketApiClient(
        transport=transport, max_attempts=2, retry_delay=0,
    )

    pages = await client.collect_tag("2", page_limit=20)

    assert pages[0].attempts == 2


@pytest.mark.asyncio
async def test_collect_tag_does_not_retry_400():
    transport = FakeTransport([response(400, {"error": "bad"})])
    client = PolymarketApiClient(transport=transport, retry_delay=0)

    with pytest.raises(PolymarketApiError) as failure:
        await client.collect_tag("2", page_limit=20)

    assert failure.value.status == 400
    assert failure.value.attempts == 1
    assert len(transport.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [b"not-json", [], {"markets": {}}, {"markets": [1]}],
)
async def test_collect_tag_rejects_invalid_response_shape(payload):
    transport = FakeTransport([response(200, payload)])
    client = PolymarketApiClient(transport=transport, retry_delay=0)

    with pytest.raises(PolymarketApiError, match="response was invalid"):
        await client.collect_tag("2", page_limit=20)


@pytest.mark.asyncio
async def test_collect_tag_rejects_repeated_cursor():
    transport = FakeTransport([
        response(200, {"markets": [], "next_cursor": "same"}),
        response(200, {"markets": [], "next_cursor": "same"}),
    ])
    client = PolymarketApiClient(transport=transport, retry_delay=0)

    with pytest.raises(PolymarketApiError, match="cursor repeated"):
        await client.collect_tag("2", page_limit=20)


@pytest.mark.asyncio
async def test_collect_tag_accepts_empty_tags():
    transport = FakeTransport([
        response(200, {"markets": [api_market(tags=[])], "next_cursor": None}),
    ])
    client = PolymarketApiClient(transport=transport, retry_delay=0)

    pages = await client.collect_tag("21", page_limit=20)

    assert pages[0].payload["markets"][0]["tags"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "market",
    [
        {"id": "1"},
        {"id": "1", "tags": None},
        api_market(tags={}),
        api_market(tags=[None]),
        api_market(tags=[{}]),
        api_market(tags=[{"slug": ""}]),
        api_market(tags=[{"slug": "   "}]),
        api_market(tags=[{"slug": 1}]),
    ],
)
async def test_collect_tag_rejects_malformed_market_tags(market):
    transport = FakeTransport([
        response(200, {"markets": [market], "next_cursor": None}),
    ])
    client = PolymarketApiClient(transport=transport, retry_delay=0)

    with pytest.raises(PolymarketApiError, match="official response was invalid") as failure:
        await client.collect_tag("21", page_limit=20)

    error = failure.value
    assert type(error).__name__ == "PolymarketTagsError"
    assert error.status == 200
    assert error.attempts == 1
    assert error.tag_id == "21"
    assert error.cursor is None


@pytest.mark.asyncio
@pytest.mark.parametrize("tag_id", ["", "abc", "-1", 2])
async def test_collect_tag_rejects_invalid_tag_id(tag_id):
    client = PolymarketApiClient(transport=FakeTransport([]), retry_delay=0)

    with pytest.raises(ValueError, match="tag ID"):
        await client.collect_tag(tag_id, page_limit=20)


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, -1, True, 21, 100])
async def test_collect_tag_rejects_invalid_page_limit(limit):
    client = PolymarketApiClient(transport=FakeTransport([]), retry_delay=0)

    with pytest.raises(ValueError, match="page limit"):
        await client.collect_tag("2", page_limit=limit)


@pytest.mark.asyncio
async def test_collect_tag_can_bound_pages_for_live_contract_checks():
    transport = FakeTransport([
        response(200, {"markets": [api_market("1")], "next_cursor": "abc"}),
    ])
    client = PolymarketApiClient(transport=transport, retry_delay=0)

    pages = await client.collect_tag("2", page_limit=1, max_pages=1)

    assert len(pages) == 1


@pytest.mark.asyncio
async def test_iter_tag_yields_pages_without_collecting_the_complete_tag():
    transport = FakeTransport([
        response(200, {"markets": [{"id": "1", "tags": None}], "next_cursor": "abc"}),
        response(200, {"markets": [api_market("2")], "next_cursor": None}),
    ])
    client = PolymarketApiClient(transport=transport, retry_delay=0)

    iterator = client.iter_tag("2", page_limit=20)
    first = await anext(iterator)

    assert first.payload["markets"][0]["id"] == "1"
    assert len(transport.requests) == 1
