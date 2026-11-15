"""Request-level enforcement for yt-dlp network activity.

yt-dlp's default networking stack dispatches every outbound HTTP(S) request
through a ``RequestDirector`` built from globally registered handlers (``requests``
in preference over ``urllib``). Validating only the initial URL — or inspecting
final ``webpage_url`` values after extraction — leaves a gap: redirects, media
format URLs, and HLS/DASH fragment requests would all escape the SSRF guard.

This module closes the gap by wiring yt-dlp through a single validating
handler (:class:`GuardedUrllibRH`) for the whole operation:

* **Fail-fast validation** of each request URL before it is issued
  (scheme, hostname, and full resolution check — mixed public/private answers
  are rejected).
* **Connection-level enforcement**: the socket is connected only to the exact
  addresses enumerated and validated in a single ``getaddrinfo`` call, so the
  address actually contacted is the validated address (no DNS rebinding /
  time-of-check-time-of-use gap). Redirect destinations are validated before
  the redirect request is issued and again at connect time.
* **Running byte budget**: every response read is accounted against a shared
  counter; exceeding the limit aborts the transfer with
  :class:`DownloadLimitExceeded`, including for fragmented/segmented media
  where the bytes are split across many requests.
* **Alternate transports cannot bypass the guard**: the director is built with
  only the guarded handler, so requests/curl_cffi are never selected, and
  ``hls_prefer_native``/``external_downloader='native'`` keep fragment and
  media downloads on the same validated path (no ffmpeg/aria2c subprocess
  fetching URLs without the SSRF check).

The module also exposes :class:`GuardedYoutubeDL`, the YoutubeDL subclass used
for every yt-dlp operation in the application.
"""
from __future__ import annotations

import functools
import http.client
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
import urllib.response
from urllib.request import (
    DataHandler,
    HTTPCookieProcessor,
    HTTPDefaultErrorHandler,
    HTTPErrorProcessor,
    UnknownHandler,
)

from yt_dlp import YoutubeDL
from yt_dlp.networking import Request, Response
from yt_dlp.networking._helper import _socket_connect
from yt_dlp.networking._urllib import (
    HTTPHandler,
    ProxyHandler,
    RedirectHandler,
    UrllibRH,
    UrllibResponseAdapter,
    handle_response_read_exceptions,
)
from yt_dlp.networking.exceptions import HTTPError, RequestError, TransportError

from ..security import check_hostname, is_public_ip, resolve_public_addrinfo, validate_public_url


class SsrfBlockedError(RequestError, OSError):
    """An outbound destination failed validation and was not contacted.

    Inherits from OSError so urllib wraps it in URLError at the socket layer,
    and from RequestError so the networking director re-raises it untouched.
    """


class DownloadLimitExceeded(RequestError):
    """The running byte budget for the download was exceeded."""


class ByteBudget:
    """Shared running byte counter for a single yt-dlp operation."""

    def __init__(self, limit: int | None):
        self.limit = limit
        self.used = 0
        self._lock = threading.Lock()

    def consume(self, n: int) -> None:
        if n <= 0:
            return
        with self._lock:
            self.used += n
            over = self.limit is not None and self.limit > 0 and self.used > self.limit
            if over:
                raise DownloadLimitExceeded(
                    f"Download exceeded maximum size of {self.limit} bytes "
                    f"(received at least {self.used} bytes)"
                )

    @property
    def exceeded(self) -> bool:
        return self.limit is not None and self.limit > 0 and self.used > self.limit


def _port_or_default(parsed) -> int:
    if parsed.port:
        return parsed.port
    return 443 if parsed.scheme == "https" else 80


def check_request_destination(url: str) -> str:
    """Fail-fast validation of an outbound request URL.

    Raises :class:`SsrfBlockedError` when the URL is not a public http(s)
    destination. The connection-time check in :func:`guarded_create_connection`
    is the authoritative enforcement; this provides a clear early error.
    """
    if not url or not isinstance(url, str):
        raise SsrfBlockedError("Request URL is required")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise SsrfBlockedError(f"Only http(s) URLs are allowed, not {parsed.scheme or '(none)'}")
    host = parsed.hostname or ""
    try:
        lowered = check_hostname(host)
    except ValueError as exc:
        raise SsrfBlockedError(str(exc)) from exc
    try:
        resolve_public_addrinfo(lowered, _port_or_default(parsed))
    except ValueError as exc:
        raise SsrfBlockedError(str(exc)) from exc
    return url


def guarded_create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
                              source_address=None, *, _create_socket_func=None):
    """``socket.create_connection`` analogue that validates every resolved
    destination before connecting, then connects to the validated addresses.

    A single ``getaddrinfo`` call feeds both validation and connection, so the
    address actually contacted is the validated one (DNS rebinding safe). Any
    prohibited address in the answer rejects the whole connection attempt.
    """
    host, port = address
    port = int(port)
    try:
        infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    except OSError as exc:
        raise SsrfBlockedError(f"Could not resolve host {host!r}: {exc}") from exc
    if not infos:
        raise SsrfBlockedError(f"Could not resolve host {host!r}: empty resolution result")
    for info in infos:
        ip = info[4][0]
        if not is_public_ip(ip):
            raise SsrfBlockedError(f"Access to non-public address {ip} is not allowed")
    if source_address is not None:
        af = socket.AF_INET if ":" not in source_address[0] else socket.AF_INET6
        infos = [info for info in infos if info[0] == af]
        if not infos:
            raise SsrfBlockedError(
                f"No remote IPv{4 if af == socket.AF_INET else 6} addresses available "
                f"for source_address {source_address!r}"
            )
    create = _create_socket_func or _socket_connect
    err: OSError | None = None
    for info in infos:
        try:
            return create(info, timeout, source_address)
        except OSError as exc:
            err = exc
    assert err is not None
    raise err


def _create_http_connection(http_class, source_address, *args, **kwargs):
    conn = http_class(*args, **kwargs)
    if hasattr(conn, "_create_connection"):
        conn._create_connection = guarded_create_connection
    if source_address is not None:
        conn.source_address = (source_address, 0)
    return conn


class GuardedHTTPHandler(HTTPHandler):
    """HTTPHandler whose connections are validated at connect time."""

    def http_open(self, req):
        conn_class = self._make_conn_class(http.client.HTTPConnection, req)
        return self.do_open(
            functools.partial(_create_http_connection, conn_class, self._source_address), req
        )

    def https_open(self, req):
        conn_class = self._make_conn_class(http.client.HTTPSConnection, req)
        return self.do_open(
            functools.partial(_create_http_connection, conn_class, self._source_address),
            req, context=self._context,
        )


class GuardedRedirectHandler(RedirectHandler):
    """Redirect handler that validates the destination before following it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            validate_public_url(newurl)
        except ValueError as exc:
            raise SsrfBlockedError(f"Blocked redirect to {newurl!r}: {exc}") from exc
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class GuardedResponse(UrllibResponseAdapter):
    """Response adapter that accounts every read against a shared ByteBudget."""

    def __init__(self, res, budget: ByteBudget):
        super().__init__(res)
        self._budget = budget

    def read(self, amt=None):
        data = super().read(amt)
        self._budget.consume(len(data or b""))
        return data


class GuardedUrllibRH(UrllibRH):
    """Urllib handler enforcing SSRF validation and a running byte budget."""

    RH_NAME = "Guarded urllib"
    _SUPPORTED_URL_SCHEMES = ("http", "https")

    def __init__(self, *, byte_budget: ByteBudget | None = None, **kwargs):
        super().__init__(**kwargs)
        self.byte_budget = byte_budget if byte_budget is not None else ByteBudget(None)

    def _create_instance(self, proxies, cookiejar, legacy_ssl_support=None):
        opener = urllib.request.OpenerDirector()
        handlers = [
            ProxyHandler(proxies),
            GuardedHTTPHandler(
                debuglevel=int(bool(self.verbose)),
                context=self._make_sslcontext(legacy_ssl_support=legacy_ssl_support),
                source_address=self.source_address,
            ),
            HTTPCookieProcessor(cookiejar),
            DataHandler(),
            UnknownHandler(),
            HTTPDefaultErrorHandler(),
            HTTPErrorProcessor(),
            GuardedRedirectHandler(),
        ]
        for handler in handlers:
            opener.add_handler(handler)
        # Delete the default user-agent header, which would otherwise apply in
        # cases where our custom HTTP handler doesn't come into play.
        opener.addheaders = []
        return opener

    def _send(self, request: Request) -> Response:
        check_request_destination(request.url)
        headers = self._get_headers(request)
        urllib_req = urllib.request.Request(
            url=request.url,
            data=request.data,
            headers=headers,
            method=request.method,
        )
        opener = self._get_instance(
            proxies=self._get_proxies(request),
            cookiejar=self._get_cookiejar(request),
            legacy_ssl_support=request.extensions.get("legacy_ssl"),
        )

        try:
            res = opener.open(urllib_req, timeout=self._calculate_timeout(request))
        except urllib.error.HTTPError as e:
            if isinstance(e.fp, (http.client.HTTPResponse, urllib.response.addinfourl)):
                # Prevent file object from being closed when urllib.error.HTTPError
                # is destroyed.
                e._closer.close_called = True
                raise HTTPError(GuardedResponse(e.fp, self.byte_budget), redirect_loop="redirect error" in str(e)) from e
            raise
        except urllib.error.URLError as e:
            cause = e.reason  # NOTE: cause may be a string
            if isinstance(cause, RequestError):
                raise cause from e
            handle_response_read_exceptions(cause)
            raise TransportError(cause=e) from e
        except (http.client.InvalidURL, ValueError) as e:
            # Validation errors
            raise RequestError(cause=e) from e
        except RequestError:
            raise
        except Exception as e:
            handle_response_read_exceptions(e)
            raise

        return GuardedResponse(res, self.byte_budget)


class GuardedYoutubeDL(YoutubeDL):
    """YoutubeDL wired exclusively through the validating request handler.

    The director is built with only :class:`GuardedUrllibRH`, so alternate
    transports (requests/curl_cffi) are never selected. Native fragment
    handling is forced by the caller (``hls_prefer_native``) so media and
    fragment requests take the same validated path.
    """

    def __init__(self, *args, byte_budget: ByteBudget | None = None, **kwargs):
        self._byte_budget = byte_budget if byte_budget is not None else ByteBudget(None)
        super().__init__(*args, **kwargs)

    @functools.cached_property
    def _request_director(self):
        return self.build_request_director(
            [functools.partial(GuardedUrllibRH, byte_budget=self._byte_budget)],
            [],
        )
