#!/usr/bin/env python3
"""Scan added/changed lines for accidental secrets.

Public GitHub remotes are intentionally allowed. The scanner avoids .env files
and redacts matched line excerpts before printing.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import subprocess
import sys
from typing import Iterable, List, Optional, Tuple

ALLOWLIST = ("git@github.com:glasses666/aurum.git",)
EXCLUDED_SUFFIXES = (".env",)
EXCLUDED_PREFIXES = (".env.",)
PATTERNS: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("bearer", re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{12,}", re.I)),
    ("credential_assignment", re.compile(r"(?i)\b(api[_-]?key|secret|token|password|credential)\b\s*[:=]\s*['\"][^'\"]{8,}['\"]")),
    ("ssh_path", re.compile(r"(?:^|[\s\"'])~?/.ssh/[^\s\"']+")),
    ("private_connection", re.compile(r"(?i)\b(?:ssh|postgres|mysql|mongodb|redis)://[^\s\"']+")),
    ("host_ip", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
)
REDACT_PATTERNS = tuple(pattern for _, pattern in PATTERNS)


def run_git(args: List[str], cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=False)


def excluded_path(path: str) -> bool:
    name = pathlib.PurePosixPath(path).name
    return name in EXCLUDED_SUFFIXES or any(name.startswith(prefix) for prefix in EXCLUDED_PREFIXES)


def redact(text: str) -> str:
    out = text
    for pattern in REDACT_PATTERNS:
        out = pattern.sub("[redacted]", out)
    return out[:180]


def diff_args_for_ci(cwd: pathlib.Path) -> List[str]:
    base_ref = os.environ.get("GITHUB_BASE_REF", "").strip()
    event_name = os.environ.get("GITHUB_EVENT_NAME", "").strip()
    if base_ref:
        run_git(["fetch", "--no-tags", "--depth=1", "origin", base_ref], cwd)
        return ["diff", "--unified=0", f"origin/{base_ref}...HEAD"]
    if event_name == "pull_request":
        return ["diff", "--unified=0", "HEAD~1..HEAD"]
    return ["diff", "--unified=0", "HEAD~1..HEAD"]


def diff_args(args: argparse.Namespace, cwd: pathlib.Path) -> List[str]:
    if args.base:
        return ["diff", "--unified=0", f"{args.base}...{args.head}"]
    if args.ci:
        return diff_args_for_ci(cwd)
    if args.staged:
        return ["diff", "--cached", "--unified=0"]
    if args.last_commit:
        return ["diff", "--unified=0", "HEAD~1..HEAD"]
    return ["diff", "--unified=0"]


def scan_diff(diff_text: str) -> List[Tuple[str, str, str]]:
    findings: List[Tuple[str, str, str]] = []
    current_path = ""
    skip_file = False
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current_path = line[6:]
            skip_file = excluded_path(current_path)
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        if skip_file:
            continue
        text = line[1:]
        if any(item in text for item in ALLOWLIST):
            continue
        for name, pattern in PATTERNS:
            if pattern.search(text):
                findings.append((current_path or "<unknown>", name, redact(text)))
    return findings


def scan_extra_files(paths: Iterable[str]) -> List[Tuple[str, str, str]]:
    findings: List[Tuple[str, str, str]] = []
    for raw_path in paths:
        path = pathlib.Path(raw_path).expanduser()
        if not path.exists() or excluded_path(path.name):
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if any(item in line for item in ALLOWLIST):
                continue
            for name, pattern in PATTERNS:
                if pattern.search(line):
                    findings.append((str(path), name, redact(line)))
    return findings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scan changed lines for accidental secrets")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--base", default="")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--ci", action="store_true")
    parser.add_argument("--staged", action="store_true")
    parser.add_argument("--last-commit", action="store_true")
    parser.add_argument("--extra-file", action="append", default=[])
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    cwd = pathlib.Path(args.repo).resolve()
    git_args = diff_args(args, cwd)
    proc = run_git(git_args, cwd)
    if proc.returncode != 0:
        print("changed-line secret scan: git diff failed with redacted stderr", file=sys.stderr)
        print(redact(proc.stderr), file=sys.stderr)
        return 2
    findings = scan_diff(proc.stdout)
    findings.extend(scan_extra_files(args.extra_file))
    if findings:
        print("changed-line secret scan findings:")
        for path, name, excerpt in findings:
            print(f"{path}: {name}: {excerpt}")
        return 1
    print("changed-line secret scan: 0 findings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
