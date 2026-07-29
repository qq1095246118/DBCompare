"""Direct, authenticated access to official Bubblemaps API endpoints."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import hashlib
import hmac
from html.parser import HTMLParser
import json
from math import isfinite
import re
import socket
from time import monotonic, time
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from getMarket.bubblemaps.tool.market_identity import TargetToken


_DEFAULT_MAP_URL = "https://v2.bubblemaps.io/map"
_DEFAULT_TIMEOUT_SECONDS = 20.0
_DEFAULT_MAX_ATTEMPTS = 3
_MAX_RETRY_AFTER_SECONDS = 300.0
_JWT_LIFETIME_SECONDS = 300
_TOP_HOLDERS_UNAVAILABLE_DETAIL = "Top holders not available for this token."
_DISCOVERY_USER_AGENT = "bubblemaps-db-compare/1.0"
_API_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
_MAP_DISCOVERY_HEADERS = {
    "accept": "text/html,application/xhtml+xml",
    "user-agent": _DISCOVERY_USER_AGENT,
}
_BUNDLE_DISCOVERY_HEADERS = {
    "accept": "application/javascript,text/javascript,*/*;q=0.1",
    "user-agent": _DISCOVERY_USER_AGENT,
}
_ENV_VALUE = re.compile(
    r"(?P<name>VITE_API_BASE_URL|VITE_API_VALIDATION_SECRET)"
    r"\s*:\s*(?P<quote>[\"'])(?P<value>(?:\\.|(?!\2).)*)\2"
)


class BubblemapsApiError(RuntimeError):
    """A deliberately non-sensitive API client failure."""

    def __init__(self, message: str, *, attempts: int = 0) -> None:
        if type(attempts) is not int or attempts < 0:
            raise ValueError("API error attempts must be a nonnegative integer")
        self.attempts = attempts
        super().__init__(message)


class TopHoldersUnavailableError(BubblemapsApiError):
    """The official API does not expose top holders for this token."""

    http_status = 400

    def __init__(self, *, attempts: int = 0) -> None:
        super().__init__("official top holders are unavailable", attempts=attempts)


@dataclass(frozen=True)
class ApiRequest:
    method: str
    url: str
    body: bytes | None
    headers: dict[str, str]


@dataclass(frozen=True)
class ApiTransportResponse:
    status: int
    body: bytes


@dataclass(frozen=True)
class RequestMetadata:
    method: str
    url: str
    status: int
    attempts: int
    request_chain: str
    request_token_address: str


@dataclass(frozen=True)
class ApiResult:
    payload: object
    metadata: RequestMetadata


@dataclass(frozen=True)
class _FrontendConfig:
    api_base_url: str
    validation_secret: str


class ApiTransport(Protocol):
    async def send(
        self,
        request: ApiRequest,
        *,
        timeout: float,
    ) -> ApiTransportResponse: ...


class UrllibTransport:
    """Dependency-free HTTPS transport that keeps blocking I/O off the event loop."""

    async def send(
        self,
        request: ApiRequest,
        *,
        timeout: float,
    ) -> ApiTransportResponse:
        return await asyncio.to_thread(self._send_sync, request, timeout)

    @staticmethod
    def _send_sync(request: ApiRequest, timeout: float) -> ApiTransportResponse:
        upstream = Request(
            request.url,
            data=request.body,
            headers=request.headers,
            method=request.method,
        )
        try:
            opener = build_opener(_NoRedirectHandler)
            with opener.open(upstream, timeout=timeout) as response:
                return ApiTransportResponse(
                    status=response.status,
                    body=response.read(),
                )
        except HTTPError as error:
            return ApiTransportResponse(status=error.code, body=error.read())


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class _AssetScripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        for name, value in attrs:
            if name.lower() == "src" and isinstance(value, str):
                self.sources.append(value)
                return


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def build_validation_jwt(
    relative_url: str,
    secret: str,
    *,
    now: int,
) -> str:
    """Build the compact HS256 token used by the current official frontend."""
    header = _base64url(b'{"alg":"HS256"}')
    payload = _base64url(
        json.dumps(
            {"data": relative_url, "exp": now + _JWT_LIFETIME_SECONDS},
            separators=(",", ":"),
        ).encode("utf-8")
    )
    signature = _base64url(
        hmac.new(
            secret.encode("utf-8"),
            f"{header}.{payload}".encode("ascii"),
            hashlib.sha256,
        ).digest()
    )
    return f"{header}.{payload}.{signature}"


def _decode_javascript_string(value: str, quote: str) -> str:
    if quote == '"':
        return json.loads(f'"{value}"')
    return bytes(value, "utf-8").decode("unicode_escape")


def _parse_frontend_config(bundle: str) -> _FrontendConfig:
    values: dict[str, str] = {}
    for match in _ENV_VALUE.finditer(bundle):
        try:
            values[match.group("name")] = _decode_javascript_string(
                match.group("value"), match.group("quote")
            )
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    base_url = values.get("VITE_API_BASE_URL")
    secret = values.get("VITE_API_VALIDATION_SECRET")
    if not base_url or not secret:
        raise BubblemapsApiError("official API configuration unavailable")
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise BubblemapsApiError("official API configuration unavailable")
    return _FrontendConfig(api_base_url=base_url.rstrip("/"), validation_secret=secret)


def _asset_urls(html: str, map_url: str) -> tuple[str, ...]:
    parser = _AssetScripts()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        raise BubblemapsApiError("official API configuration unavailable") from None
    map_parts = urlsplit(map_url)
    assets: list[str] = []
    for source in parser.sources:
        candidate = urljoin(map_url, source)
        parts = urlsplit(candidate)
        if (
            parts.scheme == "https"
            and parts.hostname == map_parts.hostname
            and parts.port == map_parts.port
            and parts.username is None
            and parts.password is None
            and parts.path.startswith("/assets/")
            and parts.path.endswith(".js")
            and not parts.fragment
        ):
            assets.append(urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, "")))
    return tuple(dict.fromkeys(assets))


def _response_text(body: object) -> str:
    if isinstance(body, bytes):
        return body.decode("utf-8")
    if isinstance(body, str):
        return body
    raise BubblemapsApiError("official API response was invalid")


def _is_timeout(error: BaseException) -> bool:
    if isinstance(error, (TimeoutError, socket.timeout)):
        return True
    if isinstance(error, URLError):
        return isinstance(error.reason, (TimeoutError, socket.timeout))
    return False


def _retry_after_delay(body: object, fallback: float) -> float:
    """Return a bounded server retry delay without retaining response details."""
    try:
        payload = json.loads(_response_text(body))
    except (
        BubblemapsApiError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        TypeError,
        ValueError,
    ):
        return fallback
    if type(payload) is not dict:
        return fallback
    retry_after = payload.get("retry_after")
    if type(retry_after) is int:
        if retry_after < 0:
            return fallback
        return float(min(retry_after, _MAX_RETRY_AFTER_SECONDS))
    if type(retry_after) is not float:
        return fallback
    if not isfinite(retry_after) or retry_after < 0:
        return fallback
    return min(retry_after, _MAX_RETRY_AFTER_SECONDS)


def _is_top_holders_unavailable(body: object) -> bool:
    try:
        payload = json.loads(_response_text(body))
    except (
        BubblemapsApiError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        TypeError,
        ValueError,
    ):
        return False
    return (
        type(payload) is dict
        and payload.get("detail") == _TOP_HOLDERS_UNAVAILABLE_DETAIL
    )


class BubblemapsApiClient:
    def __init__(
        self,
        transport: ApiTransport | None = None,
        *,
        map_url: str = _DEFAULT_MAP_URL,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        retry_delay: float = 0.25,
        clock=time,
        min_request_interval: float = 0,
        monotonic_clock=monotonic,
        sleep=None,
    ) -> None:
        if (
            not isfinite(timeout)
            or not isfinite(retry_delay)
            or not isfinite(min_request_interval)
            or timeout <= 0
            or max_attempts < 1
            or retry_delay < 0
            or min_request_interval < 0
        ):
            raise ValueError("API client retry settings are invalid")
        parsed = urlsplit(map_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError("map URL must be an HTTPS URL")
        self._transport = transport or UrllibTransport()
        self._map_url = map_url
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._retry_delay = retry_delay
        self._clock = clock
        self._min_request_interval = min_request_interval
        self._monotonic_clock = monotonic_clock
        self._sleep = asyncio.sleep if sleep is None else sleep
        self._config: _FrontendConfig | None = None
        self._config_lock = asyncio.Lock()
        self._endpoint_slot_lock = asyncio.Lock()
        self._next_endpoint_start = float("-inf")

    async def top_holders(self, target: TargetToken) -> ApiResult:
        return await self._request(
            target,
            method="POST",
            path="/addresses/token-top-holders",
            query=(("count", "300"), ("nocache", "false")),
            body={"chain": target.chain, "address": target.requested_token_address},
        )

    async def get_top_holders(self, target: TargetToken) -> ApiResult:
        return await self.top_holders(target)

    async def subgraph(
        self,
        target: TargetToken,
        ranked_addresses: list[str] | tuple[str, ...],
    ) -> ApiResult:
        return await self._request(
            target,
            method="POST",
            path="/relationships/subgraph",
            query=(
                ("whitelist_token_address", target.requested_token_address),
                ("whitelist_token_chain", target.chain),
                ("queue_whitelisted_token_map", "false"),
            ),
            body=list(ranked_addresses),
        )

    async def get_subgraph(
        self,
        target: TargetToken,
        ranked_addresses: list[str] | tuple[str, ...],
    ) -> ApiResult:
        return await self.subgraph(target, ranked_addresses)

    async def transfers(self, target: TargetToken, member_address: str) -> ApiResult:
        return await self._request(
            target,
            method="GET",
            path="/relationships/transfers",
            query=(
                ("address", member_address),
                ("whitelist_token_address", target.requested_token_address),
                ("whitelist_token_chain", target.chain),
            ),
            body=None,
        )

    async def get_transfers(self, target: TargetToken, member_address: str) -> ApiResult:
        return await self.transfers(target, member_address)

    async def _frontend_config(self) -> _FrontendConfig:
        async with self._config_lock:
            if self._config is not None:
                return self._config
            try:
                page = await self._send_frontend_request(
                    ApiRequest("GET", self._map_url, None, _MAP_DISCOVERY_HEADERS.copy())
                )
                if not 200 <= page.status < 300:
                    raise BubblemapsApiError("official API configuration unavailable")
                scripts = _asset_urls(_response_text(page.body), self._map_url)
                for script_url in scripts:
                    bundle = await self._send_frontend_request(
                        ApiRequest("GET", script_url, None, _BUNDLE_DISCOVERY_HEADERS.copy())
                    )
                    if not 200 <= bundle.status < 300:
                        continue
                    try:
                        self._config = _parse_frontend_config(_response_text(bundle.body))
                    except BubblemapsApiError:
                        continue
                    return self._config
            except BubblemapsApiError:
                raise
            except Exception:
                pass
            raise BubblemapsApiError("official API configuration unavailable")

    async def _send_frontend_request(
        self,
        request: ApiRequest,
    ) -> ApiTransportResponse:
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._transport.send(request, timeout=self._timeout)
            except Exception as error:
                if _is_timeout(error) and attempt < self._max_attempts:
                    await self._sleep(self._retry_delay)
                    continue
                raise BubblemapsApiError("official API configuration unavailable") from None
            if 500 <= response.status < 600 and attempt < self._max_attempts:
                await self._sleep(self._retry_delay)
                continue
            return response
        raise BubblemapsApiError("official API configuration unavailable")

    async def _request(
        self,
        target: TargetToken,
        *,
        method: str,
        path: str,
        query: tuple[tuple[str, str], ...],
        body: object | None,
    ) -> ApiResult:
        relative_url = path + ("?" + urlencode(query) if query else "")
        encoded_body = (
            json.dumps(body, separators=(",", ":")).encode("utf-8")
            if body is not None
            else None
        )
        refreshed = False
        attempts = 0
        while True:
            try:
                config = await self._frontend_config()
            except BubblemapsApiError:
                if attempts:
                    raise BubblemapsApiError(
                        "official API configuration unavailable",
                        attempts=attempts,
                    ) from None
                raise
            try:
                await self._wait_for_endpoint_slot()
                request = ApiRequest(
                    method=method,
                    url=config.api_base_url + relative_url,
                    body=encoded_body,
                    headers={
                        "accept": "application/json",
                        "content-type": "application/json",
                        "user-agent": _API_USER_AGENT,
                        "x-validation": build_validation_jwt(
                            relative_url,
                            config.validation_secret,
                            now=int(self._clock()),
                        ),
                    },
                )
                attempts += 1
                response = await self._transport.send(request, timeout=self._timeout)
            except Exception as error:
                if _is_timeout(error) and attempts < self._max_attempts:
                    await self._sleep(self._retry_delay)
                    continue
                raise BubblemapsApiError(
                    "official API request failed",
                    attempts=attempts,
                ) from None

            if response.status == 401:
                if refreshed:
                    raise BubblemapsApiError(
                        "official API request was unauthorized",
                        attempts=attempts,
                    )
                refreshed = True
                await self._invalidate_config(config)
                continue
            if (
                response.status == 400
                and path == "/addresses/token-top-holders"
                and _is_top_holders_unavailable(response.body)
            ):
                raise TopHoldersUnavailableError(attempts=attempts)
            if response.status == 429 and attempts < self._max_attempts:
                await self._sleep(
                    max(
                        self._retry_delay,
                        _retry_after_delay(response.body, self._retry_delay),
                    )
                )
                continue
            if 500 <= response.status < 600 and attempts < self._max_attempts:
                await self._sleep(self._retry_delay)
                continue
            if not 200 <= response.status < 300:
                raise BubblemapsApiError(
                    "official API request failed",
                    attempts=attempts,
                )
            try:
                payload = json.loads(_response_text(response.body))
            except (BubblemapsApiError, UnicodeDecodeError, json.JSONDecodeError):
                raise BubblemapsApiError(
                    "official API response was invalid",
                    attempts=attempts,
                ) from None
            return ApiResult(
                payload=payload,
                metadata=RequestMetadata(
                    method=method,
                    url=request.url,
                    status=response.status,
                    attempts=attempts,
                    request_chain=target.requested_chain,
                    request_token_address=target.requested_token_address,
                ),
            )

    async def _wait_for_endpoint_slot(self) -> None:
        if self._min_request_interval == 0:
            return
        async with self._endpoint_slot_lock:
            delay = self._next_endpoint_start - self._monotonic_clock()
            if delay > 0:
                await self._sleep(delay)
            self._next_endpoint_start = (
                self._monotonic_clock() + self._min_request_interval
            )

    async def _invalidate_config(self, expected: _FrontendConfig) -> None:
        async with self._config_lock:
            if self._config is expected:
                self._config = None
