"""Public Gamma API client with validated keyset pagination."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import socket
from time import monotonic
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


_BASE_URL = "https://gamma-api.polymarket.com/markets/keyset"
_USER_AGENT = "bubblemaps-db-compare/1.0"
_MAX_RETRY_AFTER = 300.0


@dataclass(frozen=True)
class ApiRequest:
    method: str
    url: str
    headers: dict[str, str]


@dataclass(frozen=True)
class ApiTransportResponse:
    status: int
    body: bytes
    headers: dict[str, str]


@dataclass(frozen=True)
class MarketPage:
    tag_id: str
    cursor: str | None
    captured_at: str
    request_url: str
    status: int
    attempts: int
    payload: dict[str, object]


class PolymarketApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        attempts: int = 0,
        tag_id: str | None = None,
        cursor: str | None = None,
    ) -> None:
        self.status = status
        self.attempts = attempts
        self.tag_id = tag_id
        self.cursor = cursor
        super().__init__(message)


class PolymarketTagsError(PolymarketApiError):
    def __init__(self, message: str, *, page: MarketPage) -> None:
        super().__init__(
            message,
            status=page.status,
            attempts=page.attempts,
            tag_id=page.tag_id,
            cursor=page.cursor,
        )


class ApiTransport(Protocol):
    async def send(
        self, request: ApiRequest, *, timeout: float
    ) -> ApiTransportResponse: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class UrllibTransport:
    async def send(
        self, request: ApiRequest, *, timeout: float
    ) -> ApiTransportResponse:
        return await asyncio.to_thread(self._send_sync, request, timeout)

    @staticmethod
    def _send_sync(request: ApiRequest, timeout: float) -> ApiTransportResponse:
        upstream = Request(
            request.url, headers=request.headers, method=request.method
        )
        try:
            with build_opener(_NoRedirect).open(upstream, timeout=timeout) as response:
                return ApiTransportResponse(
                    response.status,
                    response.read(),
                    {key.lower(): value for key, value in response.headers.items()},
                )
        except HTTPError as error:
            return ApiTransportResponse(
                error.code,
                error.read(),
                {key.lower(): value for key, value in error.headers.items()},
            )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _retry_after(headers: dict[str, str], fallback: float) -> float:
    value = headers.get("retry-after")
    if value is None:
        return fallback
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(parsed) or parsed < 0:
        return fallback
    return min(parsed, _MAX_RETRY_AFTER)


def _is_timeout(error: BaseException) -> bool:
    if isinstance(error, (TimeoutError, socket.timeout)):
        return True
    return isinstance(error, URLError) and isinstance(
        error.reason, (TimeoutError, socket.timeout)
    )


def _validated_payload(body: bytes) -> dict[str, object]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise PolymarketApiError("official response was invalid") from None
    if type(payload) is not dict or type(payload.get("markets")) is not list:
        raise PolymarketApiError("official response was invalid")
    if any(type(row) is not dict for row in payload["markets"]):
        raise PolymarketApiError("official response was invalid")
    cursor = payload.get("next_cursor")
    if cursor is not None and type(cursor) is not str:
        raise PolymarketApiError("official response was invalid")
    return payload


def validate_market_tags(page: MarketPage) -> None:
    markets = page.payload["markets"]
    assert type(markets) is list
    malformed = any(
        type(market.get("tags")) is not list
        or any(
            type(tag) is not dict
            or not isinstance(tag.get("slug"), str)
            or not tag["slug"].strip()
            for tag in market["tags"]
        )
        for market in markets
    )
    if malformed:
        raise PolymarketTagsError("official response was invalid", page=page)


class PolymarketApiClient:
    def __init__(
        self,
        transport: ApiTransport | None = None,
        *,
        timeout: float = 20.0,
        max_attempts: int = 3,
        retry_delay: float = 0.25,
        sleep=asyncio.sleep,
    ) -> None:
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            raise ValueError("timeout must be positive")
        if type(max_attempts) is not int or max_attempts < 1:
            raise ValueError("max attempts must be positive")
        if not isinstance(retry_delay, (int, float)) or isinstance(retry_delay, bool) or retry_delay < 0:
            raise ValueError("retry delay must be nonnegative")
        self._transport = transport or UrllibTransport()
        self._timeout = float(timeout)
        self._max_attempts = max_attempts
        self._retry_delay = float(retry_delay)
        self._sleep = sleep

    async def _page(
        self, tag_id: str, cursor: str | None, page_limit: int
    ) -> MarketPage:
        query = {
            "tag_id": tag_id,
            "active": "true",
            "closed": "false",
            "include_tag": "true",
            "limit": str(page_limit),
        }
        if cursor is not None:
            query["after_cursor"] = cursor
        url = f"{_BASE_URL}?{urlencode(query)}"
        request = ApiRequest(
            "GET", url, {"accept": "application/json", "user-agent": _USER_AGENT}
        )
        response: ApiTransportResponse | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._transport.send(request, timeout=self._timeout)
            except (TimeoutError, socket.timeout, URLError) as error:
                if not _is_timeout(error):
                    raise PolymarketApiError(
                        "official request failed", attempts=attempt,
                        tag_id=tag_id, cursor=cursor,
                    ) from None
                if attempt == self._max_attempts:
                    raise PolymarketApiError(
                        "official request failed", attempts=attempt,
                        tag_id=tag_id, cursor=cursor,
                    ) from None
                await self._sleep(self._retry_delay)
                continue
            retryable = response.status == 429 or 500 <= response.status <= 599
            if retryable and attempt < self._max_attempts:
                await self._sleep(_retry_after(response.headers, self._retry_delay))
                continue
            if response.status != 200:
                raise PolymarketApiError(
                    "official request failed", status=response.status,
                    attempts=attempt, tag_id=tag_id, cursor=cursor,
                )
            try:
                payload = _validated_payload(response.body)
            except PolymarketApiError as error:
                error.attempts = attempt
                error.status = response.status
                error.tag_id = tag_id
                error.cursor = cursor
                raise
            return MarketPage(
                tag_id, cursor, _utc_now(), url, response.status, attempt, payload
            )
        raise AssertionError("request attempt loop did not return")

    async def iter_tag(
        self,
        tag_id: str,
        *,
        page_limit: int,
        max_pages: int | None = None,
    ):
        if type(tag_id) is not str or not tag_id.isascii() or not tag_id.isdigit() or int(tag_id) < 1:
            raise ValueError("tag ID must be a positive decimal string")
        if type(page_limit) is not int or not 1 <= page_limit <= 20:
            raise ValueError("page limit must be between 1 and 20")
        if max_pages is not None and (type(max_pages) is not int or max_pages < 1):
            raise ValueError("max pages must be positive")
        page_count = 0
        seen: set[str] = set()
        cursor: str | None = None
        while True:
            page = await self._page(tag_id, cursor, page_limit)
            page_count += 1
            yield page
            if max_pages is not None and page_count >= max_pages:
                return
            next_cursor = page.payload.get("next_cursor")
            if not next_cursor:
                return
            if next_cursor in seen:
                raise PolymarketApiError(
                    "official pagination cursor repeated",
                    status=page.status,
                    attempts=page.attempts,
                    tag_id=tag_id,
                    cursor=next_cursor,
                )
            seen.add(next_cursor)
            cursor = next_cursor

    async def collect_tag(
        self,
        tag_id: str,
        *,
        page_limit: int,
        max_pages: int | None = None,
    ) -> list[MarketPage]:
        pages = []
        async for page in self.iter_tag(
            tag_id, page_limit=page_limit, max_pages=max_pages
        ):
            validate_market_tags(page)
            pages.append(page)
        return pages
