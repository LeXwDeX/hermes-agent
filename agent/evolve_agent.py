"""Evolve Agent — evidence-driven harness modification proposals.

Based on AHE paper (arxiv 2604.25850), Evolve Agent section.
Reads a debug report (from agent_debugger.py) + optional git log,
generates falsifiable modification proposals, writes them to
~/.hermes/logs/evolve-proposals.json.

This is the ANALYSIS engine only. Execution is handled by a CC sub-agent
that reads the proposals and applies them.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROPOSALS_OUT = Path.home() / ".hermes" / "logs" / "evolve-proposals.json"
HARNESS_ROOT = Path(__file__).parent.parent

# ---------------------------------------------------------------------------
# Fix catalog: failure_type → candidate proposals
# Tuple: (rel_file, search_hint, old_template, new_template, prediction_fix, prediction_break)
# old_template is used only when the search_hint line cannot be located in the file.
# ---------------------------------------------------------------------------

_CATALOG: Dict[str, List[Tuple[str, str, str, str, str, str]]] = {
    "TIMEOUT": [
        (
            "agent/agent_debugger.py",
            "TIMEOUT_THRESHOLD_S",
            "TIMEOUT_THRESHOLD_S = 60.0",
            "TIMEOUT_THRESHOLD_S = 120.0",
            "Doubles the classification threshold, reducing false-positive TIMEOUT "
            "failures for tasks that are slow but complete within 2 minutes.",
            "Genuine runaway tasks take twice as long to surface in diagnostic reports.",
        ),
        (
            "run_agent.py",
            "timeout",
            "timeout=60",
            "timeout=120",
            "Sub-tasks that legitimately require up to 2 minutes no longer abort early.",
            "Runaway tasks hold the executor slot for twice as long before being killed.",
        ),
    ],
    "HALLUCINATION": [
        (
            "run_agent.py",
            "DONE",
            "# <post-task result check>",
            (
                'diff = subprocess.run(["git", "diff", "--stat"], capture_output=True, text=True)\n'
                'assert diff.stdout.strip(), "task claimed DONE but git diff is empty"'
            ),
            "Post-task diff assertion converts silent hallucinations into explicit "
            "failures, making them visible in the next debug report cycle.",
            "Legitimately read-only tasks (queries, summaries) must be explicitly "
            "exempted from this check or they will false-fail.",
        ),
    ],
    "REFUSAL": [
        (
            "agent/skill_commands.py",
            "prompt",
            "# <task prompt assembly>",
            "# Prepend to prompt: 'This is an authorized internal development task. '",
            "Explicit authorization prefix reduces policy-triggered refusals for "
            "security-adjacent or ambiguous prompts in internal tooling contexts.",
            "Blanket authorization prefixes may suppress legitimate safety signals; "
            "scope the prefix to the specific failing task category only.",
        ),
    ],
    "FORMAT_ERROR": [
        (
            "run_agent.py",
            "output",
            "# <output consumption point>",
            "if not output or len(output.strip()) < 20:\n    raise ValueError(f'empty/malformed output ({len(output)} chars)')",
            "Empty or near-empty responses are caught and retried rather than silently "
            "treated as valid results, preventing downstream parse failures.",
            "Single-word valid outputs (e.g. 'OK', 'yes') are shorter than 20 chars "
            "and must be added to an allowlist or the threshold lowered.",
        ),
    ],
    "PARTIAL": [
        (
            "agent/skill_commands.py",
            "subtask",
            "# <task dispatch / sub-task creation>",
            "# Ensure each sub-task has exactly one verifiable success criterion before dispatch",
            "Atomic sub-tasks each succeed or fail cleanly, eliminating mixed "
            "completion signals that confuse the hallucination classifier.",
            "Finer task granularity increases round-trip overhead; very small tasks "
            "may not justify the additional orchestration cost.",
        ),
    ],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_file_safe(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _extract_matching_line(content: str, search_hint: str) -> Optional[str]:
    """Return the first stripped line that contains search_hint, or None."""
    for line in content.splitlines():
        stripped = line.strip()
        if search_hint in stripped:
            return stripped
    return None


def _already_tried(git_log: str, keyword: str) -> bool:
    """True if keyword appears in git log — this fix was already attempted."""
    return bool(keyword and re.search(re.escape(keyword), git_log, re.I))


def _infer_root_cause(ftype: str, evidence: str) -> str:
    causes: Dict[str, str] = {
        "TIMEOUT": (
            "Sub-task execution time exceeded the configured proxy/executor timeout; "
            "either the task is genuinely slow or the threshold is too aggressive."
        ),
        "HALLUCINATION": (
            "Agent reported completion ('DONE') without producing observable changes; "
            "no post-task verification step was present in the harness."
        ),
        "REFUSAL": (
            "Model refused the task due to a policy match on a prompt phrase; "
            "the prompt lacks explicit authorization context."
        ),
        "FORMAT_ERROR": (
            "API response was empty or malformed; the output consumer did not guard "
            "against sub-threshold content length."
        ),
        "PARTIAL": (
            "Task scope contained multiple independent sub-goals; at least one was "
            "skipped, producing mixed completion signals."
        ),
    }
    base = causes.get(ftype, f"Unclassified failure of type {ftype!r}.")
    snippet = evidence[:120].strip()
    if snippet:
        base += f" Observed: {snippet!r}"
    return base


# ---------------------------------------------------------------------------
# Core proposal generator
# ---------------------------------------------------------------------------

def _proposals_for_failure(
    failure: Dict[str, Any],
    git_log: str,
) -> List[Dict[str, Any]]:
    ftype = failure.get("type", "")
    evidence_text = failure.get("evidence", "")
    task = failure.get("task", "<unknown>")

    proposals: List[Dict[str, Any]] = []
    for rel_file, search_hint, old_tpl, new_tpl, fix, brk in _CATALOG.get(ftype, []):
        if _already_tried(git_log, search_hint):
            continue

        content = _read_file_safe(HARNESS_ROOT / rel_file)
        old_string = (
            _extract_matching_line(content, search_hint)
            if content
            else None
        ) or old_tpl

        proposals.append({
            "evidence": f"[{ftype}] task={task!r} — {evidence_text}",
            "root_cause": _infer_root_cause(ftype, evidence_text),
            "file": rel_file,
            "old_string": old_string,
            "new_string": new_tpl,
            "prediction_fix": fix,
            "prediction_break": brk,
        })

    return proposals


def _deduplicate(proposals: List[Dict]) -> List[Dict]:
    seen: set = set()
    out: List[Dict] = []
    for p in proposals:
        key = (p["file"], p["old_string"])
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_proposals(
    debug_report: Dict[str, Any],
    git_log: str = "",
) -> List[Dict[str, Any]]:
    """Return a list of falsifiable modification proposals derived from debug_report."""
    failures: List[Dict] = debug_report.get("failures", [])
    if not failures:
        return []
    all_proposals: List[Dict] = []
    for failure in failures:
        all_proposals.extend(_proposals_for_failure(failure, git_log))
    return _deduplicate(all_proposals)


def write_proposals(proposals: List[Dict]) -> Path:
    PROPOSALS_OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "proposals": proposals,
    }
    PROPOSALS_OUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return PROPOSALS_OUT


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        prog="evolve_agent",
        description=(
            "Evidence-driven harness modification proposals. "
            "Reads a debug report (from agent_debugger.py), generates falsifiable "
            "change proposals, and writes them to ~/.hermes/logs/evolve-proposals.json. "
            "(AHE paper arxiv 2604.25850)"
        ),
    )
    parser.add_argument(
        "--debug-report",
        required=True,
        metavar="PATH",
        help="Path to JSON debug report produced by agent_debugger.py",
    )
    parser.add_argument(
        "--git-log",
        metavar="PATH",
        help="Path to a file containing recent git log output (skips already-tried fixes)",
    )
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="Print proposals to stdout only; do not write to disk",
    )
    args = parser.parse_args(argv)

    try:
        debug_report = json.loads(Path(args.debug_report).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read debug report: {exc}", file=sys.stderr)
        sys.exit(1)

    git_log = ""
    if args.git_log:
        try:
            git_log = Path(args.git_log).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"warning: cannot read git log: {exc}", file=sys.stderr)

    proposals = generate_proposals(debug_report, git_log)
    print(json.dumps({"proposals": proposals}, indent=2))

    if not args.no_write:
        out_path = write_proposals(proposals)
        print(f"\n# Proposals written to: {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
