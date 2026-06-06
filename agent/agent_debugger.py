"""Agent Debugger — converts CC sub-agent execution traces into structured diagnostic reports.

Based on the AHE paper (arxiv 2604.25850), Agent Debugger section.
Reads nginx proxy access/error logs and raw CC stdout, classifies failures,
and writes a JSON diagnostic report to ~/.hermes/logs/cc-debug-{timestamp}.json.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_ACCESS_LOG = "/var/log/nginx/cc-proxy-access.log"
DEFAULT_ERROR_LOG = "/var/log/nginx/cc-proxy-error.log"
TIMEOUT_THRESHOLD_S = 60.0

# Nginx combined log format: ip - - [date] "METHOD path HTTP/x.x" status bytes "ref" "ua" rt=N.NNN
_ACCESS_RE = re.compile(
    r'(?P<ip>\S+) \S+ \S+ \[(?P<time>[^\]]+)\] '
    r'"(?P<method>\S+) (?P<path>\S+) \S+" '
    r'(?P<status>\d{3}) \d+ "[^"]*" "[^"]*"'
    r'(?:.*?rt=(?P<rt>[\d.]+))?'
)

_REFUSAL_PATTERNS = re.compile(
    r"i(?:'m| am) not going to|i cannot|i(?:'m| am) unable to|"
    r"i(?:'ll| will) not|i must decline|i(?:'m| am) sorry, but i(?:'m| am) not|"
    r"that(?:'s| is) (not something i|outside)|"
    r"i(?:'m| am) designed to avoid|violates? (my|anthropic)",
    re.I,
)

_FORMAT_ERROR_MIN_LEN = 20  # fewer chars = likely malformed/empty output


# ---------------------------------------------------------------------------
# Log parsers
# ---------------------------------------------------------------------------

def _parse_access_log(path: str) -> List[Dict[str, Any]]:
    """Parse nginx access log lines into structured dicts."""
    entries: List[Dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = _ACCESS_RE.search(line)
                if not m:
                    continue
                rt = float(m.group("rt")) if m.group("rt") else None
                entries.append({
                    "time": m.group("time"),
                    "method": m.group("method"),
                    "path": m.group("path"),
                    "status": int(m.group("status")),
                    "rt": rt,
                    "raw": line.rstrip(),
                })
    except FileNotFoundError:
        pass
    return entries


def _parse_error_log(path: str) -> List[str]:
    """Return non-empty lines from the nginx error log."""
    lines: List[str] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.rstrip()
                if line:
                    lines.append(line)
    except FileNotFoundError:
        pass
    return lines


def _read_stdin_text() -> str:
    """Read all of stdin (for piped CC output)."""
    return sys.stdin.read()


# ---------------------------------------------------------------------------
# Failure classifiers
# ---------------------------------------------------------------------------

def _classify_access_entry(entry: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Return a failure dict if the access log entry represents a failure."""
    status = entry["status"]
    rt = entry["rt"]
    path = entry.get("path", "")

    if rt is not None and rt > TIMEOUT_THRESHOLD_S:
        return {
            "type": "TIMEOUT",
            "evidence": f"rt={rt:.3f}s > {TIMEOUT_THRESHOLD_S}s",
            "task": path,
        }
    if status >= 500:
        return {
            "type": "FORMAT_ERROR",
            "evidence": f"HTTP {status}",
            "task": path,
        }
    if status == 408 or status == 504:
        return {
            "type": "TIMEOUT",
            "evidence": f"HTTP {status}",
            "task": path,
        }
    return None


def _classify_stdin_output(text: str, task_hint: str = "<stdin>") -> List[Dict[str, str]]:
    """Classify failures from raw CC stdout text."""
    failures: List[Dict[str, str]] = []

    # TIMEOUT: exit code 124 signal in output
    if re.search(r"\bexit\s+(?:code\s+)?124\b", text, re.I):
        failures.append({
            "type": "TIMEOUT",
            "evidence": "exit code 124 found in output",
            "task": task_hint,
        })

    # REFUSAL
    if _REFUSAL_PATTERNS.search(text):
        snippet = _find_snippet(_REFUSAL_PATTERNS, text)
        failures.append({
            "type": "REFUSAL",
            "evidence": snippet,
            "task": task_hint,
        })

    # HALLUCINATION: claimed DONE but git diff empty
    if re.search(r"\bDONE\b", text):
        diff = _git_diff_stat()
        if diff is not None and diff.strip() == "":
            failures.append({
                "type": "HALLUCINATION",
                "evidence": "CC output contains 'DONE' but git diff shows no changes",
                "task": task_hint,
            })

    # FORMAT_ERROR: output too short to be actionable
    stripped = text.strip()
    if stripped and len(stripped) < _FORMAT_ERROR_MIN_LEN:
        failures.append({
            "type": "FORMAT_ERROR",
            "evidence": f"output too short ({len(stripped)} chars): {stripped!r}",
            "task": task_hint,
        })

    # PARTIAL: contains both success markers and skip/skip markers
    has_done = bool(re.search(r"\bDONE\b|\bcompleted?\b", text, re.I))
    has_skip = bool(re.search(r"\bskip(ping|ped)?\b|\bnot (done|completed)\b", text, re.I))
    if has_done and has_skip and not any(f["type"] == "HALLUCINATION" for f in failures):
        failures.append({
            "type": "PARTIAL",
            "evidence": "output contains both completion and skip markers",
            "task": task_hint,
        })

    return failures


def _find_snippet(pattern: re.Pattern, text: str, context: int = 80) -> str:
    """Return a short snippet around the first regex match."""
    m = pattern.search(text)
    if not m:
        return ""
    start = max(0, m.start() - 20)
    end = min(len(text), m.end() + context)
    return text[start:end].replace("\n", " ").strip()


def _git_diff_stat() -> Optional[str]:
    """Return git diff --stat output, or None if git is unavailable."""
    try:
        result = subprocess.run(
            ["git", "diff", "--stat"],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Recommendations engine
# ---------------------------------------------------------------------------

def _generate_recommendations(failures: List[Dict[str, str]]) -> List[str]:
    types = {f["type"] for f in failures}
    recs: List[str] = []
    if "TIMEOUT" in types:
        recs.append("Increase proxy timeout or break long tasks into smaller sub-tasks.")
    if "HALLUCINATION" in types:
        recs.append("Add a post-task git diff check to verify CC actually produced changes.")
    if "REFUSAL" in types:
        recs.append("Review task prompts for policy-sensitive language; rephrase or add authorization context.")
    if "FORMAT_ERROR" in types:
        recs.append("Check CC output parsing logic; guard against empty/malformed responses.")
    if "PARTIAL" in types:
        recs.append("Decompose tasks further so each sub-task is atomic and fully completable.")
    if not recs:
        recs.append("No actionable failures detected.")
    return recs


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------

def build_report(
    access_log: Optional[str],
    error_log: Optional[str],
    stdin_text: Optional[str],
) -> Dict[str, Any]:
    failures: List[Dict[str, str]] = []
    total_requests = 0

    # Access log
    if access_log:
        entries = _parse_access_log(access_log)
        total_requests += len(entries)
        for entry in entries:
            f = _classify_access_entry(entry)
            if f:
                failures.append(f)

    # Error log (each line = a potential failure signal)
    error_lines: List[str] = []
    if error_log:
        error_lines = _parse_error_log(error_log)
        for line in error_lines:
            if re.search(r"upstream timed out|504|499", line, re.I):
                failures.append({
                    "type": "TIMEOUT",
                    "evidence": line[:200],
                    "task": "<nginx-error-log>",
                })

    # Stdin (raw CC output)
    if stdin_text:
        total_requests += 1
        failures.extend(_classify_stdin_output(stdin_text))

    total = max(total_requests, 1)
    success_count = total - len(failures)
    success_rate = round(max(success_count, 0) / total, 4)

    return {
        "total_requests": total_requests,
        "failures": failures,
        "success_rate": success_rate,
        "recommendations": _generate_recommendations(failures),
    }


def write_report(report: Dict[str, Any]) -> Path:
    """Write report JSON to ~/.hermes/logs/cc-debug-{timestamp}.json."""
    log_dir = Path.home() / ".hermes" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = log_dir / f"cc-debug-{ts}.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        prog="agent_debugger",
        description=(
            "Convert CC sub-agent execution traces into structured diagnostic reports. "
            "Reads nginx proxy logs and/or piped CC stdout, classifies failures, "
            "and writes a JSON report to ~/.hermes/logs/. "
            "(AHE paper arxiv 2604.25850)"
        ),
    )
    parser.add_argument(
        "--access-log",
        metavar="PATH",
        default=DEFAULT_ACCESS_LOG,
        help=f"Path to nginx cc-proxy access log (default: {DEFAULT_ACCESS_LOG})",
    )
    parser.add_argument(
        "--error-log",
        metavar="PATH",
        default=DEFAULT_ERROR_LOG,
        help=f"Path to nginx cc-proxy error log (default: {DEFAULT_ERROR_LOG})",
    )
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="Read raw CC stdout from stdin (pipe mode)",
    )
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="Print report to stdout only; do not write to disk",
    )
    args = parser.parse_args(argv)

    stdin_text: Optional[str] = None
    if args.stdin:
        stdin_text = _read_stdin_text()

    report = build_report(
        access_log=args.access_log,
        error_log=args.error_log,
        stdin_text=stdin_text,
    )

    output = json.dumps(report, indent=2)
    print(output)

    if not args.no_write:
        out_path = write_report(report)
        print(f"\n# Report written to: {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
