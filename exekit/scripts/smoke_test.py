"""ExecKit smoke test — exercises the public API on a running server.

Usage:
    python scripts/smoke_test.py [--base-url http://localhost:8000]

Prints PASS/FAIL per check and exits non-zero if any FAIL. Safe to run
against a server without SOLARI_API_KEY: tests adapt to both cases
(configured: full execution + session round trip; unconfigured: clean 503s).
"""

import argparse
import sys
import urllib.error
import urllib.request
import uuid

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    suffix = f"  ({detail})" if detail and not ok else ""
    print(f"{'PASS' if ok else 'FAIL'}  {name}{suffix}")


def request(method: str, path: str, key: str | None = None, body: dict | None = None):
    """Returns (status_code, parsed_json_or_None). Never raises on HTTP errors."""
    url = BASE_URL + path
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-API-Key"] = key
    data = None
    if body is not None:
        import json

        data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return resp.status, (parse_json(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, (parse_json(raw) if raw else None)


def parse_json(raw: bytes):
    import json

    try:
        return json.loads(raw.decode())
    except Exception:
        return None


def main() -> int:
    global BASE_URL
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    args = parser.parse_args()
    BASE_URL = args.base_url.rstrip("/")

    print(f"ExecKit smoke test against {BASE_URL}\n")

    # A. /health
    status, body = request("GET", "/health")
    check("A. GET /health returns 200", status == 200, f"got {status}")
    solari_configured = bool(body and body.get("solari_configured"))
    print(f"      server reports solari_configured={solari_configured}\n")

    # B. key request
    email = f"smoke-{uuid.uuid4().hex[:8]}@example.com"
    status, body = request("POST", "/keys/request", body={"email": email})
    raw_key = (body or {}).get("api_key")
    check("B. POST /keys/request returns 200 + api_key", status == 200 and raw_key,
          f"got {status}")

    # C. /keys/me
    status, me = request("GET", "/keys/me", key=raw_key)
    check("C. GET /keys/me returns credits", status == 200
          and isinstance((me or {}).get("credits"), int), f"got {status}")
    credits_before = (me or {}).get("credits")

    # D. executions without key -> 401 missing_api_key
    status, body = request("POST", "/executions", body={"code": "print(1)"})
    code = ((body or {}).get("error") or {}).get("code")
    check("D. POST /executions without key -> 401", status == 401, f"got {status}")
    check("D2. error code is missing_api_key", code == "missing_api_key", f"got {code}")

    # E. invalid key -> 401 invalid_api_key
    status, body = request("POST", "/executions", key="ek_live_" + "x" * 40,
                           body={"code": "print(1)"})
    code = ((body or {}).get("error") or {}).get("code")
    check("E. POST /executions invalid key -> 401", status == 401, f"got {status}")
    check("E2. error code is invalid_api_key", code == "invalid_api_key", f"got {code}")

    # F. empty code -> 400
    status, body = request("POST", "/executions", key=raw_key, body={"code": "   "})
    code = ((body or {}).get("error") or {}).get("code")
    check("F. POST /executions empty code -> 400", status == 400, f"got {status}")
    check("F2. error code is empty_code", code == "empty_code", f"got {code}")

    # G. real execution
    status, body = request("POST", "/executions", key=raw_key, body={"code": "print('smoke')"})
    if solari_configured:
        ok = status == 200 and (body or {}).get("status") in ("completed", "failed", "timeout")
        check("G. POST /executions returns an execution result", ok,
              f"got {status} {body}")
        execution = body or {}
    else:
        code = ((body or {}).get("error") or {}).get("code")
        check("G. POST /executions -> 503 solari_unconfigured (Solari missing)",
              status == 503 and code == "solari_unconfigured", f"got {status} {body}")
        execution = {}

    # H. credits decrement when the execution was admitted
    if solari_configured and execution:
        _, me2 = request("GET", "/keys/me", key=raw_key)
        check("H. credits decremented after execution",
              me2.get("credits", 0) < credits_before,
              f"{credits_before} -> {me2.get('credits')}")
    else:
        _, me2 = request("GET", "/keys/me", key=raw_key)
        check("H. credits unchanged (refunded on infra failure)",
              me2.get("credits") == credits_before,
              f"{credits_before} -> {me2.get('credits')}")

    # I/J. session round trip
    status, body = request("POST", "/sessions", key=raw_key)
    if solari_configured:
        session_id = (body or {}).get("session_id")
        check("I1. POST /sessions returns session_id", status == 201 and session_id,
              f"got {status} {body}")
        status, body = request("GET", f"/sessions/{session_id}", key=raw_key)
        check("I2. GET /sessions/{id} returns info",
              status == 200 and (body or {}).get("session_id") == session_id, f"got {status}")
        status, body = request("DELETE", f"/sessions/{session_id}", key=raw_key)
        check("I3. DELETE /sessions/{id} kills session",
              status == 200 and (body or {}).get("status") == "killed", f"got {status}")
    else:
        code = ((body or {}).get("error") or {}).get("code")
        check("J. POST /sessions -> clean error (no crash)",
              status in (401, 402, 403, 410, 429, 501, 502, 503, 504)
              and isinstance(code, str) and code != "internal",
              f"got {status} {body}")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", ", ".join(FAIL))
        return 1
    return 0


BASE_URL = "http://localhost:8000"

if __name__ == "__main__":
    sys.exit(main())
