#!/usr/bin/env python3
"""Aurum VPS network preflight.

Checks whether a candidate cloud node can reach Polymarket public read endpoints
and the public CLOB market WebSocket host. Uses only the Python standard library.

This script is read-only: it performs HTTPS GET requests and a WebSocket opening
handshake only. It does not authenticate, place orders, or store credentials.

Python compatibility: 3.6+.
"""

import argparse
import base64
import json
import os
import random
import socket
import ssl
import struct
import time
import urllib.error
import urllib.request


DEFAULT_HTTPS_ENDPOINTS = [
    ("gamma_markets", "https://gamma-api.polymarket.com/markets?limit=1"),
    ("clob_markets", "https://clob.polymarket.com/markets?limit=1"),
    ("data_trades", "https://data-api.polymarket.com/trades?limit=1"),
]

DEFAULT_WS_ENDPOINTS = [
    ("clob_market_ws", "wss://ws-subscriptions-clob.polymarket.com/ws/market"),
]


def make_result(name, target, ok, status, elapsed_ms, detail=""):
    return {
        "name": name,
        "target": target,
        "ok": bool(ok),
        "status": str(status),
        "elapsed_ms": int(elapsed_ms),
        "detail": str(detail or ""),
    }


def now_ms():
    return int(time.perf_counter() * 1000)


def dns_qname(name):
    out = b""
    for part in name.split("."):
        out += bytes([len(part)]) + part.encode("ascii")
    return out + b"\0"


def skip_dns_name(data, offset):
    # DNS names may be label sequences or compressed pointers.
    while offset < len(data):
        length = data[offset]
        if length == 0:
            return offset + 1
        if length & 0xC0 == 0xC0:
            return offset + 2
        offset += 1 + length
    raise ValueError("truncated DNS name")


def query_a_record(host, dns_server, timeout):
    tid = random.randrange(65536)
    packet = struct.pack("!HHHHHH", tid, 0x0100, 1, 0, 0, 0)
    packet += dns_qname(host) + struct.pack("!HH", 1, 1)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(packet, (dns_server, 53))
        data, _ = sock.recvfrom(4096)
    finally:
        sock.close()

    if len(data) < 12:
        raise ValueError("short DNS response")
    rid, flags, qdcount, ancount, _nscount, _arcount = struct.unpack("!HHHHHH", data[:12])
    if rid != tid:
        raise ValueError("DNS transaction id mismatch")
    rcode = flags & 0x000F
    if rcode != 0:
        raise ValueError("DNS rcode {}".format(rcode))

    offset = 12
    for _ in range(qdcount):
        offset = skip_dns_name(data, offset)
        offset += 4

    addrs = []
    for _ in range(ancount):
        offset = skip_dns_name(data, offset)
        if offset + 10 > len(data):
            raise ValueError("truncated DNS answer")
        rtype, rclass, _ttl, rdlen = struct.unpack("!HHIH", data[offset : offset + 10])
        offset += 10
        rdata = data[offset : offset + rdlen]
        offset += rdlen
        if rtype == 1 and rclass == 1 and rdlen == 4:
            addrs.append(socket.inet_ntoa(rdata))
    if not addrs:
        raise ValueError("no A records in DNS response")
    return addrs


def install_dns_override(dns_server, timeout):
    original_getaddrinfo = socket.getaddrinfo
    cache = {}

    def patched_getaddrinfo(host, port, family=0, socktype=0, proto=0, flags=0):
        try:
            socket.inet_aton(host)
            return original_getaddrinfo(host, port, family, socktype, proto, flags)
        except (OSError, TypeError):
            pass

        if family not in (0, socket.AF_UNSPEC, socket.AF_INET):
            return original_getaddrinfo(host, port, family, socktype, proto, flags)

        try:
            addrs = cache.get(host)
            if addrs is None:
                addrs = query_a_record(host, dns_server, timeout)
                cache[host] = addrs
            result = []
            use_socktype = socktype or socket.SOCK_STREAM
            use_proto = proto or socket.IPPROTO_TCP
            for addr in addrs:
                result.append((socket.AF_INET, use_socktype, use_proto, "", (addr, port)))
            return result
        except Exception:
            return original_getaddrinfo(host, port, family, socktype, proto, flags)

    socket.getaddrinfo = patched_getaddrinfo


def check_https(name, url, timeout):
    start = now_ms()
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "aurum-vps-preflight/0.2 (+read-only)",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read(4096)
            elapsed = now_ms() - start
            status = str(getattr(resp, "status", "unknown"))
            content_type = resp.headers.get("content-type", "")
            return make_result(
                name,
                url,
                200 <= int(status) < 400,
                status,
                elapsed,
                "content_type={}; sample_bytes={}".format(content_type, len(body)),
            )
    except urllib.error.HTTPError as exc:
        elapsed = now_ms() - start
        sample = exc.read(512).decode("utf-8", "replace") if exc.fp else ""
        return make_result(
            name,
            url,
            False,
            "HTTP {}".format(exc.code),
            elapsed,
            sample[:300].replace("\n", " "),
        )
    except Exception as exc:  # report concrete network errors.
        elapsed = now_ms() - start
        return make_result(
            name,
            url,
            False,
            type(exc).__name__,
            elapsed,
            str(exc)[:500],
        )


def parse_wss_url(url):
    if not url.startswith("wss://"):
        raise ValueError("only wss:// supported: {}".format(url))
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


def check_wss_handshake(name, url, timeout):
    start = now_ms()
    try:
        host, port, path = parse_wss_url(url)
        raw = socket.create_connection((host, port), timeout=timeout)
        raw.settimeout(timeout)
        context = ssl.create_default_context()
        sock = context.wrap_socket(raw, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            "GET {} HTTP/1.1\r\n".format(path)
            + "Host: {}\r\n".format(host)
            + "Upgrade: websocket\r\n"
            + "Connection: Upgrade\r\n"
            + "Sec-WebSocket-Key: {}\r\n".format(key)
            + "Sec-WebSocket-Version: 13\r\n"
            + "User-Agent: aurum-vps-preflight/0.2 (+read-only)\r\n"
            + "\r\n"
        )
        sock.sendall(request.encode("ascii"))
        response = sock.recv(2048).decode("latin1", "replace")
        sock.close()
        elapsed = now_ms() - start
        first_line = response.splitlines()[0] if response.splitlines() else "no response"
        ok = " 101 " in first_line or first_line.endswith(" 101 Switching Protocols")
        return make_result(
            name,
            url,
            ok,
            first_line,
            elapsed,
            "websocket opening handshake only",
        )
    except Exception as exc:
        elapsed = now_ms() - start
        return make_result(
            name,
            url,
            False,
            type(exc).__name__,
            elapsed,
            str(exc)[:500],
        )


def summarize(results):
    ok_count = sum(1 for r in results if r["ok"])
    total = len(results)
    ok_latencies = [r["elapsed_ms"] for r in results if r["ok"]]
    worst_latency = max(ok_latencies) if ok_latencies else None
    https_results = [r for r in results if r["target"].startswith("https://")]
    ws_results = [r for r in results if r["target"].startswith("wss://")]
    https_ok = all(r["ok"] for r in https_results)
    ws_ok = all(r["ok"] for r in ws_results)
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


def main():
    parser = argparse.ArgumentParser(description="Aurum VPS network preflight")
    parser.add_argument("--timeout", type=float, default=10.0, help="per-check timeout seconds")
    parser.add_argument("--dns-server", default="", help="optional DNS server IP for A-record resolution override")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args()

    if args.dns_server:
        install_dns_override(args.dns_server, min(args.timeout, 5.0))

    results = []
    for name, url in DEFAULT_HTTPS_ENDPOINTS:
        results.append(check_https(name, url, args.timeout))
    for name, url in DEFAULT_WS_ENDPOINTS:
        results.append(check_wss_handshake(name, url, args.timeout))

    summary = summarize(results)
    payload = {"summary": summary, "dns_server_override": args.dns_server or None, "results": results}

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("Aurum VPS network preflight: {}".format(summary["verdict"]))
        print("OK: {}/{}; worst_ok_latency_ms={}".format(summary["ok"], summary["total"], summary["worst_ok_latency_ms"]))
        for r in results:
            marker = "OK" if r["ok"] else "FAIL"
            print("[{}] {}: {} in {}ms".format(marker, r["name"], r["status"], r["elapsed_ms"]))
            if r["detail"]:
                print("      {}".format(r["detail"]))

    return 0 if summary["verdict"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
