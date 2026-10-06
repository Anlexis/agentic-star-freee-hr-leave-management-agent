"""AgentCore Platform v1.0 - freee HR REST API v1 client.

Service layer: a thin wrapper around the freee HR (leave-management SaaS) REST
API v1 leave endpoints. Contains NO business logic, NO routing, and NO
credentials - the integration token is passed in per call by the node (which
reads it via ctx.secrets). This module imports no framework/SDK internals -
pure stdlib (import isolation).

LIMITATION (deliberate, documented):
    The DEFAULT transport is a deterministic, NETWORK-FREE stub. It returns
    stable freee-HR-shaped responses (a ``leave_balances`` list for balance
    lookups; a request-receipt shape with a synthetic ``request_id`` echo for
    submissions; a status shape for status checks - all derived
    deterministically from the request) so the pipeline is runnable and
    testable without a live freee HR tenant or the ``requests`` package - it
    does NOT perform a live freee HR call. The template never fakes a live
    call; the limitation is documented instead.

    To perform real freee HR calls, inject live transports (requests-based
    ``post`` / ``get``) at construction time; the method contracts are modelled
    on the freee HR REST API v1 resource families (employees / approval
    requests) and the live transport adapter owns final path fidelity, so no
    business-logic change is needed to go live. A live transport also requires
    a real integration token (see CallFreeeHrApiNode - the stub runs without
    one because no request ever leaves the process).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

# A transport callable: (url, headers, json_body) -> (status_code, response_dict)
Transport = Callable[[str, "dict[str, Any]", "dict[str, Any]"], "tuple[int, dict[str, Any]]"]

_BASE_URL = "https://api.freee.co.jp/hr/api/v1"


class FreeeHrApiError(Exception):
    """Raised when the freee HR REST API returns a non-2xx status."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"freee HR API error {status_code}: {message}")


class FreeeHrClient:
    """freee HR REST API v1 leave-management client.

    Args:
        base_url: freee HR API base URL (default https://api.freee.co.jp/hr/api/v1).
        post/get: optional injected transports (tests or a live client).
            When none is injected, a deterministic NETWORK-FREE stub is used
            (see the module docstring - it returns a stable freee-HR shape
            without a live freee HR call).
    """

    def __init__(
        self,
        base_url: str = _BASE_URL,
        *,
        post: Transport | None = None,
        get: Transport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._post = post
        self._get = get

    # -- transport mode --------------------------------------------------------

    @property
    def uses_stub_transport(self) -> bool:
        """True when NO live transport is injected (the network-free default)."""
        return self._post is None and self._get is None

    # -- auth ----------------------------------------------------------------

    def _headers(self, api_token: str) -> "dict[str, str]":
        """Build the freee HR REST API v1 auth headers (OAuth2 bearer token).

        api_token is supplied per-call by the node (from ctx.secrets); it is
        never persisted on the instance or logged.
        """
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_token}",
        }

    # -- deterministic stub transport (default; NO network) -------------------

    def _stub_transport(
        self, url: str, headers: "dict[str, str]", json_body: "dict[str, Any]"
    ) -> "tuple[int, dict[str, Any]]":
        """Deterministic, network-free stub - returns a stable freee HR shape.

        NOT a live call. Synthetic ids/numbers are derived from the request so
        the response is stable and inspectable. See the module docstring for
        the limitation and how to inject live transports.
        """
        seed = url + "|" + json.dumps(json_body, sort_keys=True, ensure_ascii=False, default=str)
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        op = json_body.get("_freee_op")
        if op == "balance":
            employee_id = str(json_body.get("employee_id", "")) or f"e-{digest[:8]}"
            remaining = round((int(digest[:3], 16) % 200) / 10.0, 1)
            taken = int(digest[3:5], 16) % 30
            return 200, {
                "employee_id": employee_id,
                "leave_balances": [
                    {
                        "leave_type": "paid_holiday",
                        "remaining_days": remaining,
                        "taken_days": taken,
                    }
                ],
                "_stub": True,  # marks the network-free stub response
            }
        if op == "status":
            request_id = str(json_body.get("request_id", "")) or f"lr-{digest[:8]}"
            status = "approved" if int(digest[0], 16) % 2 == 0 else "in_progress"
            return 200, {
                "request_id": request_id,
                "status": status,
                "_stub": True,  # marks the network-free stub response
            }
        # Leave-request submission - receipt shape with a synthetic request_id
        # echo so the caller can reference the created request without a
        # follow-up lookup.
        return 200, {
            "request_id": f"lr-{digest[:8]}",
            "status": "in_progress",
            "_stub": True,  # marks the network-free stub response
        }

    def _resolve(self, injected: Transport | None) -> Transport:
        return injected or self._stub_transport

    # -- public API ---------------------------------------------------------

    def get_leave_balance(self, employee_id: str, api_token: str, company_id: str = "") -> "dict[str, Any]":
        """GET employees/<id>/holidays - look up an employee's leave balances.

        Returns the parsed response dict (containing ``leave_balances``).
        ``company_id`` scopes the tenant when configured (freee HR requests are
        company-scoped; the stub echoes it back untouched). Raises
        FreeeHrApiError on a non-2xx status.
        """
        url = f"{self._base_url}/employees/{employee_id}/holidays"
        transport = self._resolve(self._get)
        body: dict[str, Any] = {"_freee_op": "balance", "employee_id": employee_id}
        if company_id:
            body["company_id"] = company_id
        status, resp = transport(url, self._headers(api_token), body)
        if not (200 <= status < 300):
            raise FreeeHrApiError(status, _err_message(resp))
        return resp

    def submit_leave_request(self, payload: "dict[str, Any]", api_token: str, company_id: str = "") -> "dict[str, Any]":
        """POST approval_requests/paid_holidays - submit a new leave request.

        ``payload`` is the ``{"leave_request": {...}}`` request body assembled
        by InferFreeeHrFieldsNode. Returns the parsed response dict (request
        receipt with ``request_id``). Raises FreeeHrApiError on a non-2xx
        status.
        """
        url = f"{self._base_url}/approval_requests/paid_holidays"
        transport = self._resolve(self._post)
        body: dict[str, Any] = dict(payload)
        if company_id:
            body["company_id"] = company_id
        status, resp = transport(url, self._headers(api_token), body)
        if not (200 <= status < 300):
            raise FreeeHrApiError(status, _err_message(resp))
        return resp

    def get_request_status(self, request_id: str, api_token: str, company_id: str = "") -> "dict[str, Any]":
        """GET approval_requests/paid_holidays/<id> - check a leave request's status.

        Returns the parsed response dict (containing ``status``). Raises
        FreeeHrApiError on a non-2xx status.
        """
        url = f"{self._base_url}/approval_requests/paid_holidays/{request_id}"
        transport = self._resolve(self._get)
        body: dict[str, Any] = {"_freee_op": "status", "request_id": request_id}
        if company_id:
            body["company_id"] = company_id
        status, resp = transport(url, self._headers(api_token), body)
        if not (200 <= status < 300):
            raise FreeeHrApiError(status, _err_message(resp))
        return resp


def _err_message(body: Any) -> str:
    """Extract a human-readable error message from a freee HR error body."""
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            return "; ".join(str(e) for e in errors)
        msg = body.get("message")
        if msg:
            return str(msg)
    return str(body)
