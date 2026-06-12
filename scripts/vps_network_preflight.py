#!/usr/bin/env python3
"""Aurum VPS network preflight.

Checks whether a candidate cloud node can reach Polymarket public read endpoints
and the public CLOB market WebSocket host. Uses only the Python standard library.

This script is read-only: it performs HTTPS GET requests and a WebSocket opening
handshake only. It does not authenticate, place orders, or store credentials.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any


DEFAULT_HTTPS_ENDPOINTS = [
    ("gamma_markets", "https://gamma-api.polymarket.com/markets?limit=1"),
    ("clob_markets", "https://clob.polymarket.com/markets?limit=1"),
    ("data_trades", "https://data-api.polymarket.com/trades?limit=1"),
]

DEFAULT_WS_ENDPOINTS = [
    ("clob_market_ws", "wss://ws-subscriptions-clob.polymarket.com/ws/market"),
]


@dataclass
class CheckResult:
    name: str
    target: str
    ok: bool
    status: str
    elapsed_ms: int
    detail: str = ""


def _now_ms() -> int:
    return int(time.perf_counter() * 1000)


def check_https(name: str, url: str, timeout: float) -> CheckResult:
    start = _now_ms()
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "aurum-vps-preflight/0.1 (+read-only)",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(4096)
            elapsed = _now_ms() - start
            status = str(getattr(resp, "status", "unknown"))
            content_type = resp.headers.get("content-type", "")
            return CheckResult(
                name=name,
                target=url,
                ok=200 <= int(status) < 400,
                status=status,
                elapsed_ms=elapsed,
                detail=f"content_type={content_type}; sample_bytes={len(body)}",
            )
    except urllib.error.HTTPError as exc:
        elapsed = _now_ms() - start
        sample = exc.read(512).decode("utf-8", "replace") if exc.fp else ""
        return CheckResult(
            name=name,
            target=url,
            ok=False,
            status=f"HTTP {exc.code}",
            elapsed_ms=elapsed,
            detail=sample[:300].replace("\n", " "),
        )
    except Exception as exc:  # noqa: BLE001 - report concrete network errors.
        elapsed = _now_ms() - start
        return CheckResult(
            name=name,
            target=url,
            ok=False,
            status=type(exc).__name__,
            elapsed_ms=elapsed,
            detail=str(exc)[:500],
        )


def parse_wss_url(url: str) -> tuple[str, int, str]:
    if not url.startswith("wss://"):
        raise ValueError(f"only wss:// supported: {url}")
    rest = url[len("wss://") :]
    host_port, _, path = rest.partition("/")
    path = "/" + path if path else "/"
    if ":" in host_port:
        host, port_s = host_port.rsplit(":", 1)
        port = int(port_s)
    else:
        host = host_port
        port = 443
    return host, port, path


def check_wss_handshake(name: str, url: str, timeout: float) -> CheckResult:
    start = _now_ms()
    try:
        host, port, path = parse_wss_url(url)
        raw = socket.create_connection((host, port), timeout=timeout)
        raw.settimeout(timeout)
        context = ssl.create_default_context()
        sock = context.wrap_socket(raw, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "User-Agent: aurum-vps-preflight/0.1 (+read-only)\r\n"
            "\r\n"
        )
        sock.sendall(request.encode("ascii"))
        response = sock.recv(2048).decode("latin1", "replace")
        sock.close()
        elapsed = _now_ms() - start
        first_line = response.splitlines()[0] if response.splitlines() else "no response"
        ok = " 101 " in first_line or first_line.endswith(" 101 Switching Protocols")
        return CheckResult(
            name=name,
            target=url,
            ok=ok,
            status=first_line,
            elapsed_ms=elapsed,
            detail="websocket opening handshake only",
        )
    except Exception as exc:  # noqa: BLE001
        elapsed = _now_ms() - start
        return CheckResult(
            name=name,
            target=url,
            ok=False,
            status=type(exc).__name__,
            elapsed_ms=elapsed,
            detail=str(exc)[:500],
        )


def summarize(results: list[CheckResult]) -> dict[str, Any]:
    ok_count = sum(1 for r in results if r.ok)
    total = len(results)
    worst_latency = max((r.elapsed_ms for r in results if r.ok), default=None)
    https_ok = all(r.ok for r in results if r.target.startswith("https://"))
    ws_ok = all(r.ok for r in results if r.target.startswith("wss://"))
    verdict = "pass" if ok_count == total else "fail"
    if https_ok and not ws_ok:
        verdict = "partial_https_only"
    return {
        "verdict": verdict,
        "ok": ok_count,
        "total": total,
        "worst_ok_latency_ms": worst_latency,
        "https_ok": https_ok,
        "websocket_ok": ws_ok,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Aurum VPS network preflight")
    parser.add_argument("--timeout", type=float, default=10.0, help="per-check timeout seconds")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args()

    results: list[CheckResult] = []
    for name, url in DEFAULT_HTTPS_ENDPOINTS:
        results.append(check_https(name, url, args.timeout))
    for name, url in DEFAULT_WS_ENDPOINTS:
        results.append(check_wss_handshake(name, url, args.timeout))

    summary = summarize(results)
    payload = {"summary": summary, "results": [asdict(r) for r in results]}

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"Aurum VPS network preflight: {summary['verdict']}")
        print(f"OK: {summary['ok']}/{summary['total']}; worst_ok_latency_ms={summary['worst_ok_latency_ms']}")
        for r in results:
            marker = "OK" if r.ok else "FAIL"
            print(f"[{marker}] {r.name}: {r.status} in {r.elapsed_ms}ms")
            if r.detail:
                print(f"      {r.detail}")

    return 0 if summary["verdict"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
