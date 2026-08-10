"""Thin Alpaca adapter. HUMAN REVIEW REQUIRED.

Credentials come from the runtime credential store written by the setup
UI (stage 7) - never from the repository, never logged. In the build
sandbox they arrive via environment variables for live paper
verification only; nothing in this module ever prints or persists them.

Paper account facts verified live 2026-08-10 (STRATEGY-PROPOSALS.md
section 1): shorting_enabled=false, multiplier=1, PDT fields absent,
corporate actions live at /v1/corporate-actions (not /v2/).

Retry policy mirrors the TRAPS.md EDGAR rule: transient 5xx and network
errors retry with backoff; a 4xx NEVER retries. Order submission is made
retry-safe by always sending a client_order_id - a duplicate submit of
the same client_order_id is rejected by Alpaca instead of double-filling.
"""

import os
import time

import httpx

PAPER_BASE_URL = "https://paper-api.alpaca.markets"
DATA_BASE_URL = "https://data.alpaca.markets"

_RETRIES = 3
_BACKOFF_S = 1.0


class BrokerError(Exception):
    """Network failure or 5xx after retries. Carries url + status only -
    never headers, never credentials."""

    def __init__(self, message: str, status_code: int | None = None,
                 body: dict | str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class OrderRejected(Exception):
    """4xx on an order action. body is the broker's verbatim parsed
    response so the caller can record it (house rule 3)."""

    def __init__(self, status_code: int, body):
        super().__init__(f"order rejected: HTTP {status_code}")
        self.status_code = status_code
        self.body = body


class Broker:
    """All Alpaca calls go through this one class so stress-tester has a
    single seam to attack and tests have a single seam to stub
    (transport= accepts an httpx.MockTransport)."""

    def __init__(self, key_id: str, secret_key: str,
                 base_url: str = PAPER_BASE_URL,
                 data_url: str = DATA_BASE_URL,
                 transport: httpx.BaseTransport | None = None,
                 backoff_s: float = _BACKOFF_S):
        self._base_url = base_url
        self._data_url = data_url
        self._backoff_s = backoff_s
        self._client = httpx.Client(
            headers={
                "APCA-API-KEY-ID": key_id,
                "APCA-API-SECRET-KEY": secret_key,
                "Accept": "application/json",
            },
            timeout=10.0,
            transport=transport,
        )

    def __repr__(self):  # keys must never leak through repr/logging
        return f"Broker(base_url={self._base_url!r}, key_id=<redacted>)"

    @classmethod
    def from_env(cls, **kwargs) -> "Broker":
        key = os.environ.get("ALPACA_KEY")
        secret = os.environ.get("ALPACA_SECRET_KEY")
        if not key or not secret:
            raise BrokerError(
                "ALPACA_KEY / ALPACA_SECRET_KEY not present in environment")
        return cls(key, secret, **kwargs)

    def close(self):
        self._client.close()

    # ------------------------------------------------------------ plumbing

    def _request(self, method: str, url: str, *, json_body=None, params=None,
                 reject_as_order: bool = False):
        for attempt in range(_RETRIES + 1):
            try:
                resp = self._client.request(method, url, json=json_body,
                                            params=params)
            except httpx.HTTPError as exc:
                # httpx error text can embed the URL but not headers; keep
                # the message to exception class + url anyway.
                if attempt < _RETRIES:
                    time.sleep(self._backoff_s * (2 ** attempt))
                    continue
                raise BrokerError(
                    f"{type(exc).__name__} on {method} {url} "
                    f"after {_RETRIES + 1} attempts") from None

            if resp.status_code >= 500:
                if attempt < _RETRIES:
                    time.sleep(self._backoff_s * (2 ** attempt))
                    continue
                raise BrokerError(f"HTTP {resp.status_code} on {method} {url} "
                                  f"after {_RETRIES + 1} attempts",
                                  status_code=resp.status_code,
                                  body=_safe_json(resp))
            if resp.status_code >= 400:
                body = _safe_json(resp)
                if reject_as_order:
                    raise OrderRejected(resp.status_code, body)
                raise BrokerError(f"HTTP {resp.status_code} on {method} {url}",
                                  status_code=resp.status_code, body=body)
            if resp.status_code == 204:
                return None
            try:
                return resp.json()
            except ValueError:
                # A 200 whose body is empty, truncated or HTML (a proxy
                # or gateway page). Raised as BrokerError so every
                # caller's fail-closed path applies instead of a
                # JSONDecodeError escaping the layer (stress-tester
                # defect 2). The raw text goes with it, capped - house
                # rule 3, every zero keeps its upstream response.
                raise BrokerError(
                    f"unparseable body on {method} {url}",
                    status_code=resp.status_code,
                    body=resp.text[:2000]) from None
        raise BrokerError(f"unreachable retry state on {method} {url}")

    def _request_object(self, *args, **kwargs) -> dict:
        """A response this endpoint documents as a JSON object. Anything
        else (null, a list, a bare string) is a broker error, never a
        value the caller has to type-check (stress-tester defect 2)."""
        value = self._request(*args, **kwargs)
        if not isinstance(value, dict):
            raise BrokerError(
                f"expected a JSON object, got {type(value).__name__}",
                body=value)
        return value

    def _request_object_list(self, *args, **kwargs) -> list[dict]:
        """A response documented as a JSON array of objects. An element
        that is not an object is refused rather than dropped: a resting
        stop we cannot parse must never read as 'no stop resting'."""
        value = self._request(*args, **kwargs)
        if not isinstance(value, list) or any(
                not isinstance(item, dict) for item in value):
            raise BrokerError(
                f"expected a JSON array of objects, got {type(value).__name__}",
                body=value)
        return value

    # ------------------------------------------------------------- trading

    def get_account(self) -> dict:
        return self._request_object("GET", f"{self._base_url}/v2/account")

    def get_clock(self) -> dict:
        return self._request_object("GET", f"{self._base_url}/v2/clock")

    def get_positions(self) -> list[dict]:
        return self._request_object_list("GET",
                                         f"{self._base_url}/v2/positions")

    def get_open_orders(self) -> list[dict]:
        return self._request_object_list(
            "GET", f"{self._base_url}/v2/orders",
            params={"status": "open", "limit": 500})

    def get_order(self, broker_order_id: str) -> dict:
        return self._request_object(
            "GET", f"{self._base_url}/v2/orders/{broker_order_id}")

    def get_order_by_client_id(self, client_order_id: str) -> dict:
        return self._request_object(
            "GET", f"{self._base_url}/v2/orders:by_client_order_id",
            params={"client_order_id": client_order_id})

    def submit_order(self, *, symbol: str, qty: str, side: str,
                     order_type: str, time_in_force: str,
                     client_order_id: str,
                     stop_price: str | None = None,
                     limit_price: str | None = None) -> dict:
        body = {
            "symbol": symbol, "qty": qty, "side": side, "type": order_type,
            "time_in_force": time_in_force,
            "client_order_id": client_order_id,
        }
        if stop_price is not None:
            body["stop_price"] = stop_price
        if limit_price is not None:
            body["limit_price"] = limit_price
        return self._request_object("POST", f"{self._base_url}/v2/orders",
                                    json_body=body, reject_as_order=True)

    def cancel_order(self, broker_order_id: str) -> None:
        self._request("DELETE", f"{self._base_url}/v2/orders/{broker_order_id}")

    # ---------------------------------------------------------------- data

    def get_latest_quote(self, symbol: str) -> dict:
        return self._request_object(
            "GET", f"{self._data_url}/v2/stocks/{symbol}/quotes/latest")


def _safe_json(resp: httpx.Response):
    try:
        return resp.json()
    except ValueError:
        return resp.text
