#!/usr/bin/env python3
"""Re-run the reviewer's own reproduction against head *and* the merge base.

The agent is the hypothesis generator; this is the part that decides whether a
hypothesis survives.  It is deliberately the smallest possible version of
zero-loop's stage-3 cascade:

    head fails, base passes  -> the change introduced it      (verified)
    head fails, base fails   -> it was already there          (withheld)
    head passes, base fails  -> the change *fixes* it         (withheld)
    head passes, base passes -> the probe did not reproduce it (withheld)

A finding carries the exact command it was demonstrated with (``probe``).  The
command must exit non-zero when the bug is present.  No probe, no evidence: a
claim without one is withheld rather than published on trust.

Runs as an unprivileged uid with no token and no model key, in throw-away
worktrees the entrypoint prepared.  Stdlib only: the review image ships
python3 for goby's own summaries, not a package index.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

SCHEMA = 1
MAX_PROBE_CHARS = 4000
SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}
SEVERITY_ALIASES = {"critical": "high", "blocker": "high", "info": "low", "minor": "low"}

VERIFIED = "verified"
WITHHELD = "withheld"

#: A probe that cannot even start says nothing about the change.  These are the
#: setup failures seen in practice; the list is a heuristic and the record says
#: so, because a real bug that shells out to a missing binary looks the same.
SETUP_ERROR = re.compile(
    r"(ModuleNotFoundError|ImportError|command not found|No such file or directory|"
    r"Cannot find module|is not recognized as an internal or external command|"
    r"error: could not compile|cargo: not found|go: command not found|"
    r"fixture '[^']*' not found|no tests ran|ERROR: file or directory not found)",
    re.IGNORECASE,
)

#: The probe's environment is an allowlist, not the verifier's environment: the
#: code under review is untrusted, and anything not named here is not its business.
ENV_ALLOW = (
    "PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "SHELL", "USER",
    "LOGNAME", "TERM", "TZ", "VIRTUAL_ENV", "PYTHONPATH", "NODE_PATH",
    "PIP_BREAK_SYSTEM_PACKAGES", "GIT_TERMINAL_PROMPT",
)


@dataclass
class Finding:
    path: str
    line: int
    title: str
    severity: str
    probe: str = ""
    detail: str = ""

    @property
    def key(self) -> tuple[str, int, str]:
        return (self.path, self.line, self.title.lower())


@dataclass
class Outcome:
    state: str  # pass | fail | setup_error | timeout
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False


@dataclass
class Record:
    path: str
    line: int
    title: str
    severity: str
    status: str
    kind: str
    detail: str
    head: str
    base: str
    exit_head: int | None = None
    exit_base: int | None = None
    duration_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "line": self.line,
            "title": self.title,
            "severity": self.severity,
            "status": self.status,
            "kind": self.kind,
            "detail": self.detail,
            "head": self.head,
            "base": self.base,
            "exit_head": self.exit_head,
            "exit_base": self.exit_base,
            "duration_ms": self.duration_ms,
        }


def tail(text: str, limit: int = 1200) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def trim(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def coerce(raw: Any) -> Finding | None:
    """Mirror goby's own contract: path, 1-based line, non-empty title."""
    if not isinstance(raw, dict):
        return None
    path, line, title = raw.get("path"), raw.get("line"), raw.get("title")
    if not isinstance(path, str) or not path.strip():
        return None
    if not isinstance(line, int) or isinstance(line, bool) or line < 1:
        return None
    if not isinstance(title, str) or not title.strip():
        return None
    severity = trim(raw.get("severity")).lower()
    severity = SEVERITY_ALIASES.get(severity, severity)
    if severity not in SEVERITY_RANK:
        severity = "medium"
    probe = trim(raw.get("probe"))
    return Finding(
        path=path.strip(),
        line=line,
        title=title.strip(),
        severity=severity,
        probe=probe,
        detail=trim(raw.get("detail")),
    )


def read_findings(path: Path) -> tuple[list[Finding], list[str]]:
    """Every ``kind == "findings"`` record, deduplicated, in emission order."""
    degraded: list[str] = []
    if not path.exists():
        return [], ["the agent wrote no findings record"]
    merged: dict[tuple[str, int, str], Finding] = {}
    order: list[tuple[str, int, str]] = []
    bad = 0
    for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            degraded.append(f"findings.jsonl line {number} is not JSON")
            continue
        if not isinstance(record, dict) or record.get("kind") != "findings":
            continue
        entries = record.get("findings")
        if not isinstance(entries, list):
            degraded.append(f"findings.jsonl line {number} has no findings array")
            continue
        for entry in entries:
            finding = coerce(entry)
            if finding is None:
                bad += 1
                continue
            if len(finding.probe) > MAX_PROBE_CHARS or "\0" in finding.probe:
                finding.probe = ""
                degraded.append(f"{finding.path}:{finding.line} has an unusable probe")
            current = merged.get(finding.key)
            if current is None:
                merged[finding.key] = finding
                order.append(finding.key)
            elif not current.probe and finding.probe:
                current.probe = finding.probe
    if bad:
        degraded.append(f"{bad} finding(s) were off-contract and could not be verified")
    return [merged[key] for key in order], degraded


def probe_env(home: str) -> dict[str, str]:
    env = {name: os.environ[name] for name in ENV_ALLOW if name in os.environ}
    env["PATH"] = env.get("PATH") or "/usr/local/bin:/usr/bin:/bin"
    env["HOME"] = home
    env["TMPDIR"] = "/tmp"
    env["PIP_BREAK_SYSTEM_PACKAGES"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def run_probe(command: str, cwd: Path, timeout_s: float, home: str) -> Outcome:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            ["bash", "-lc", command],
            cwd=str(cwd),
            env=probe_env(home),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=max(1.0, timeout_s),
            start_new_session=True,
        )
    except subprocess.TimeoutExpired as expired:
        return Outcome(
            state="timeout",
            exit_code=None,
            stdout=tail(expired.stdout if isinstance(expired.stdout, str) else ""),
            stderr=tail(expired.stderr if isinstance(expired.stderr, str) else ""),
            duration_ms=int((time.monotonic() - started) * 1000),
            timed_out=True,
        )
    except OSError as exc:
        return Outcome(
            state="setup_error", exit_code=None, stdout="",
            stderr=f"{type(exc).__name__}: {exc}",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    duration = int((time.monotonic() - started) * 1000)
    if completed.returncode == 0:
        state = "pass"
    elif completed.returncode in (126, 127):
        state = "setup_error"
    elif SETUP_ERROR.search(completed.stderr or ""):
        state = "setup_error"
    else:
        state = "fail"
    return Outcome(
        state=state,
        exit_code=completed.returncode,
        stdout=tail(completed.stdout),
        stderr=tail(completed.stderr),
        duration_ms=duration,
    )


def classify(head: Outcome, base: Outcome) -> tuple[str, str]:
    """``(status, kind)`` from the two runs.  See the module docstring."""
    if head.state == "timeout":
        return WITHHELD, "timeout"
    if head.state == "setup_error":
        return WITHHELD, "setup_error"
    if head.state == "fail":
        if base.state == "pass":
            return VERIFIED, "regression"
        if base.state == "fail":
            return WITHHELD, "pre_existing"
        return WITHHELD, f"base_{base.state}"
    # The probe passed on head: the bug is not present there, whatever base did.
    if base.state == "fail":
        return WITHHELD, "fix_confirmed"
    if base.state == "pass":
        return WITHHELD, "not_reproduced"
    return WITHHELD, f"base_{base.state}"


def explain(kind: str, record: Record, head: Outcome, base: Outcome) -> str:
    where = f"{record.path}:{record.line}"
    if kind == "regression":
        return f"the probe fails on {where} at head ({head.exit_code}) and passes at the merge base"
    if kind == "pre_existing":
        return f"the probe fails at head and at the merge base; this change did not introduce it"
    if kind == "fix_confirmed":
        return "the probe fails at the merge base and passes at head; this change fixes the defect"
    if kind == "not_reproduced":
        return "the probe passes at head and at the merge base; it does not demonstrate the claim"
    if kind == "setup_error":
        return f"the probe could not run here: {tail(head.stderr, 240) or 'no diagnostic'}"
    if kind == "timeout":
        return f"the probe exceeded its {head.duration_ms // 1000}s wall clock"
    if kind.startswith("base_"):
        return f"head ran, but the merge-base run was {kind[5:]}: {tail(base.stderr, 240) or 'no diagnostic'}"
    return kind


def verify(
    findings: Sequence[Finding],
    *,
    head_dir: Path,
    base_dir: Path,
    out_dir: Path,
    head_sha: str,
    diff_sha256: str,
    mode: str,
    max_probes: int,
    probe_timeout_s: float,
    budget_s: float,
    is_fixture: bool = False,
) -> dict[str, Any]:
    started = time.monotonic()
    degraded: list[str] = []
    records: list[Record] = []
    log_lines: list[str] = []

    ranked = sorted(
        enumerate(findings),
        key=lambda pair: (SEVERITY_RANK.get(pair[1].severity, 1), pair[0]),
    )
    ordered = [finding for _, finding in ranked]

    probes = 0
    for finding in ordered:
        record = Record(
            path=finding.path, line=finding.line, title=finding.title,
            severity=finding.severity, status=WITHHELD, kind="no_probe",
            detail="", head="skipped", base="skipped",
        )
        if not finding.probe:
            record.detail = "no reproduction command was recorded, so the claim was not verified"
            records.append(record)
            continue
        remaining = budget_s - (time.monotonic() - started)
        if remaining < 5:
            record.kind = "skipped"
            record.detail = "the verification budget was spent before this finding was reached"
            records.append(record)
            degraded.append("probe_budget_exhausted: some findings were not verified")
            continue
        if probes >= max_probes:
            record.kind = "skipped"
            record.detail = f"only the first {max_probes} findings are probed"
            records.append(record)
            degraded.append(f"probe_cap_reached: more than {max_probes} probes were requested")
            continue
        probes += 1

        timeout = max(5.0, min(probe_timeout_s, remaining - 2))
        log_lines.append(f"=== {finding.path}:{finding.line} [{finding.severity}] {finding.title}")
        log_lines.append(f"$ {finding.probe}")
        head = run_probe(finding.probe, head_dir, timeout, str(head_dir))
        record.head = head.state
        record.exit_head = head.exit_code
        record.duration_ms = head.duration_ms
        log_lines.append(f"head {head.state} rc={head.exit_code} {head.duration_ms}ms")
        if head.stdout:
            log_lines.append(f"head stdout: {head.stdout}")
        if head.stderr:
            log_lines.append(f"head stderr: {head.stderr}")

        if head.state in ("timeout", "setup_error"):
            # A probe that cannot run on head says nothing about the base.
            base = Outcome("skipped", None, "", "", 0)
            record.base = "skipped"
        else:
            base = run_probe(finding.probe, base_dir, timeout, str(base_dir))
            record.base = base.state
            record.exit_base = base.exit_code
            record.duration_ms += base.duration_ms
            log_lines.append(f"base {base.state} rc={base.exit_code} {base.duration_ms}ms")
            if base.stdout:
                log_lines.append(f"base stdout: {base.stdout}")
            if base.stderr:
                log_lines.append(f"base stderr: {base.stderr}")

        record.status, record.kind = classify(head, base)
        record.detail = explain(record.kind, record, head, base)
        if record.kind == "pre_existing":
            degraded.append("pre_existing_finding: a finding reproduces on the merge base")
        if record.kind == "setup_error" and not is_fixture:
            degraded.append("probe_setup_error: at least one probe could not run in this environment")
        records.append(record)

    elapsed_ms = int((time.monotonic() - started) * 1000)
    verdict = {
        "schema": SCHEMA,
        "head_sha": head_sha,
        "diff_sha256": diff_sha256,
        "mode": mode,
        "records": [record.as_dict() for record in records],
        "degraded": sorted(set(degraded)),
        "elapsed_ms": elapsed_ms,
        "counts": {
            "verified": sum(1 for r in records if r.status == VERIFIED),
            "withheld": sum(1 for r in records if r.status == WITHHELD),
            "probed": probes,
        },
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write(out_dir / "verdict.json", json.dumps(verdict, indent=2) + "\n")
    _atomic_write(out_dir / "probes.log", "\n".join(log_lines) + ("\n" if log_lines else ""))
    return verdict


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Re-run a reviewer's probes against head and base")
    parser.add_argument("--findings", required=True, type=Path)
    parser.add_argument("--head-dir", required=True, type=Path)
    parser.add_argument("--base-dir", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--head", required=True)
    parser.add_argument("--diff-sha256", required=True)
    parser.add_argument("--mode", default="required", choices=("required", "advisory"))
    parser.add_argument("--max-probes", type=int, default=8)
    parser.add_argument("--probe-timeout", type=float, default=90.0)
    parser.add_argument("--budget", type=float, default=300.0)
    parser.add_argument("--fixture", action="store_true", help="suppress environment-specific degraded reasons")
    args = parser.parse_args(argv)

    for name in ("head_dir", "base_dir"):
        if not getattr(args, name).is_dir():
            print(f"verify: {name} {getattr(args, name)} is not a directory", file=sys.stderr)
            return 2
    if args.max_probes < 0:
        print("verify: --max-probes must be non-negative", file=sys.stderr)
        return 2

    findings, degraded = read_findings(args.findings)
    verdict = verify(
        findings,
        head_dir=args.head_dir,
        base_dir=args.base_dir,
        out_dir=args.out,
        head_sha=args.head,
        diff_sha256=args.diff_sha256,
        mode=args.mode,
        max_probes=args.max_probes,
        probe_timeout_s=args.probe_timeout,
        budget_s=args.budget,
        is_fixture=args.fixture,
    )
    if degraded:
        verdict["degraded"] = sorted(set(list(verdict["degraded"]) + degraded))
        _atomic_write(args.out / "verdict.json", json.dumps(verdict, indent=2) + "\n")
    print(json.dumps({"verified": verdict["counts"]["verified"], "withheld": verdict["counts"]["withheld"],
                      "probed": verdict["counts"]["probed"], "degraded": verdict["degraded"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
