#!/usr/bin/env python3
"""Tests for the verification pass.

Real directories, real child processes, no mocks: the four classifications are
the product, and a stubbed `subprocess.run` would only prove the stub agrees
with itself.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify as verifier  # noqa: E402


def write(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def findings_file(root: Path, entries: list[dict]) -> Path:
    path = root / "findings.jsonl"
    path.write_text(json.dumps({"kind": "findings", "findings": entries}) + "\n", encoding="utf-8")
    return path


class Trees:
    """One head tree and one base tree, each with a `bug` marker file or not."""

    def __init__(self, root: Path):
        self.root = root
        self.head = root / "head"
        self.base = root / "base"
        self.head.mkdir(parents=True, exist_ok=True)
        self.base.mkdir(parents=True, exist_ok=True)

    def mark(self, tree: Path) -> None:
        write(tree / "bug", "present\n")


class CoerceTests(unittest.TestCase):
    def test_accepts_the_contract_and_normalises_severity(self):
        finding = verifier.coerce({
            "path": "src/a.py", "line": 4, "title": "boom",
            "severity": "critical", "probe": "  pytest -q  ",
        })
        self.assertIsNotNone(finding)
        assert finding is not None
        self.assertEqual(finding.severity, "high")
        self.assertEqual(finding.probe, "pytest -q")
        self.assertEqual(finding.key, ("src/a.py", 4, "boom"))

    def test_rejects_off_contract_entries(self):
        for entry in (
            {},
            {"path": "", "line": 1, "title": "x"},
            {"path": "a", "line": 0, "title": "x"},
            {"path": "a", "line": 1, "title": "  "},
            {"path": "a", "line": "1", "title": "x"},
                    ):
            self.assertIsNone(verifier.coerce(entry), entry)

    def test_unknown_severity_falls_back_to_medium(self):
        finding = verifier.coerce({"path": "a", "line": 1, "title": "x", "severity": "urgent"})
        self.assertIsNotNone(finding)
        assert finding is not None
        self.assertEqual(finding.severity, "medium")


class ReadFindingsTests(unittest.TestCase):
    def test_deduplicates_and_keeps_the_first_probe(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = findings_file(Path(temporary), [
                {"path": "a.py", "line": 2, "title": "Same", "severity": "low"},
                {"path": "a.py", "line": 2, "title": "same", "severity": "high", "probe": "exit 1"},
            ])
            findings, degraded = verifier.read_findings(path)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].severity, "low")
            self.assertEqual(findings[0].probe, "exit 1")
            self.assertEqual(degraded, [])

    def test_a_broken_line_is_degraded_not_fatal(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "findings.jsonl"
            path.write_text(
                "{not json}\n"
                + json.dumps({"kind": "findings", "findings": [
                    {"path": "a.py", "line": 1, "title": "ok", "probe": "exit 1"},
                    {"path": "", "line": 1, "title": "bad"},
                ]}) + "\n",
                encoding="utf-8",
            )
            findings, degraded = verifier.read_findings(path)
            self.assertEqual([f.title for f in findings], ["ok"])
            self.assertEqual(len(degraded), 2)

    def test_missing_file_is_a_degraded_reason(self):
        with tempfile.TemporaryDirectory() as temporary:
            findings, degraded = verifier.read_findings(Path(temporary) / "nope.jsonl")
            self.assertEqual(findings, [])
            self.assertEqual(len(degraded), 1)

    def test_an_unusable_probe_is_dropped_and_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = findings_file(Path(temporary), [
                {"path": "a.py", "line": 1, "title": "x", "probe": "x" * (verifier.MAX_PROBE_CHARS + 1)},
            ])
            findings, degraded = verifier.read_findings(path)
            self.assertEqual(findings[0].probe, "")
            self.assertTrue(any("unusable probe" in reason for reason in degraded))


class ClassificationTests(unittest.TestCase):
    """The four outcomes, plus the two ways a probe fails to say anything."""

    def run_case(self, head_has_bug: bool, base_has_bug: bool, probe: str = "test ! -f bug",
                 timeout: float = 20.0) -> dict:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trees = Trees(root)
            if head_has_bug:
                trees.mark(trees.head)
            if base_has_bug:
                trees.mark(trees.base)
            verdict = verifier.verify(
                [verifier.Finding("src/a.py", 3, "bug", "high", probe)],
                head_dir=trees.head, base_dir=trees.base, out_dir=root / "out",
                head_sha="a" * 40, diff_sha256="b" * 64, mode="required",
                max_probes=5, probe_timeout_s=timeout, budget_s=60.0,
            )
            return verdict["records"][0]

    def test_head_fails_base_passes_is_a_regression(self):
        record = self.run_case(head_has_bug=True, base_has_bug=False)
        self.assertEqual(record["status"], "verified")
        self.assertEqual(record["kind"], "regression")
        self.assertEqual((record["head"], record["base"]), ("fail", "pass"))

    def test_failing_on_both_sides_is_pre_existing(self):
        record = self.run_case(head_has_bug=True, base_has_bug=True)
        self.assertEqual(record["status"], "withheld")
        self.assertEqual(record["kind"], "pre_existing")

    def test_passing_on_head_failing_on_base_is_a_fix(self):
        record = self.run_case(head_has_bug=False, base_has_bug=True)
        self.assertEqual(record["status"], "withheld")
        self.assertEqual(record["kind"], "fix_confirmed")

    def test_passing_on_both_sides_did_not_reproduce(self):
        record = self.run_case(head_has_bug=False, base_has_bug=False)
        self.assertEqual(record["status"], "withheld")
        self.assertEqual(record["kind"], "not_reproduced")

    def test_a_missing_command_is_a_setup_error_not_a_bug(self):
        record = self.run_case(True, True, probe="definitely-not-a-command-xyz")
        self.assertEqual(record["status"], "withheld")
        self.assertEqual(record["kind"], "setup_error")

    def test_a_probe_that_hangs_times_out(self):
        record = self.run_case(True, True, probe="sleep 30", timeout=1)
        self.assertEqual(record["status"], "withheld")
        self.assertEqual(record["kind"], "timeout")


class VerdictTests(unittest.TestCase):
    def test_no_probe_is_withheld_with_a_reason(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trees = Trees(root)
            verdict = verifier.verify(
                [verifier.Finding("a.py", 1, "claim", "high")],
                head_dir=trees.head, base_dir=trees.base, out_dir=root / "out",
                head_sha="a" * 40, diff_sha256="b" * 64, mode="required",
                max_probes=5, probe_timeout_s=5.0, budget_s=30.0,
            )
            record = verdict["records"][0]
            self.assertEqual((record["status"], record["kind"]), ("withheld", "no_probe"))
            self.assertIn("no reproduction command", record["detail"])

    def test_the_cap_bounds_probing_and_says_so(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trees = Trees(root)
            trees.mark(trees.head)
            findings = [verifier.Finding(f"f{i}.py", 1, f"c{i}", "high", "test ! -f bug") for i in range(3)]
            verdict = verifier.verify(
                findings, head_dir=trees.head, base_dir=trees.base, out_dir=root / "out",
                head_sha="a" * 40, diff_sha256="b" * 64, mode="required",
                max_probes=1, probe_timeout_s=5.0, budget_s=30.0,
            )
            kinds = [record["kind"] for record in verdict["records"]]
            self.assertEqual(kinds.count("regression"), 1)
            self.assertEqual(kinds.count("skipped"), 2)
            self.assertTrue(any("probe_cap_reached" in reason for reason in verdict["degraded"]))

    def test_every_finding_gets_a_record_and_the_files_are_written(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trees = Trees(root)
            trees.mark(trees.head)
            out = root / "out"
            verdict = verifier.verify(
                [
                    verifier.Finding("a.py", 1, "verified", "high", "test ! -f bug"),
                    verifier.Finding("b.py", 1, "unverified", "low"),
                ],
                head_dir=trees.head, base_dir=trees.base, out_dir=out,
                head_sha="a" * 40, diff_sha256="b" * 64, mode="required",
                max_probes=5, probe_timeout_s=5.0, budget_s=30.0,
            )
            self.assertEqual(len(verdict["records"]), 2)
            self.assertEqual(verdict["counts"], {"verified": 1, "withheld": 1, "probed": 1})
            written = json.loads((out / "verdict.json").read_text(encoding="utf-8"))
            self.assertEqual(written["schema"], verifier.SCHEMA)
            self.assertEqual(written["head_sha"], "a" * 40)
            self.assertTrue((out / "probes.log").exists())

    def test_the_verdict_never_contains_the_probe_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trees = Trees(root)
            out = root / "out"
            os.environ["GOBY_TEST_SECRET"] = "should-not-leak"
            try:
                verdict = verifier.verify(
                    [verifier.Finding("a.py", 1, "c", "high", "echo ${GOBY_TEST_SECRET:-absent} && exit 1")],
                    head_dir=trees.head, base_dir=trees.base, out_dir=out,
                    head_sha="a" * 40, diff_sha256="b" * 64, mode="required",
                    max_probes=1, probe_timeout_s=5.0, budget_s=30.0,
                )
            finally:
                del os.environ["GOBY_TEST_SECRET"]
            self.assertNotIn("should-not-leak", json.dumps(verdict))
            self.assertIn("absent", (out / "probes.log").read_text(encoding="utf-8"))


class CommandLineTests(unittest.TestCase):
    def test_missing_directories_are_a_usage_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            code = verifier.main([
                "--findings", str(Path(temporary) / "f.jsonl"),
                "--head-dir", str(Path(temporary) / "nope"),
                "--base-dir", str(Path(temporary) / "nope2"),
                "--out", str(Path(temporary) / "out"),
                "--head", "a" * 40, "--diff-sha256", "b" * 64,
            ])
            self.assertEqual(code, 2)

    def test_it_writes_a_verdict_and_prints_a_summary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trees = Trees(root)
            trees.mark(trees.head)
            path = findings_file(root, [{"path": "a.py", "line": 1, "title": "x", "probe": "test ! -f bug"}])
            out = root / "out"
            code = verifier.main([
                "--findings", str(path), "--head-dir", str(trees.head), "--base-dir", str(trees.base),
                "--out", str(out), "--head", "a" * 40, "--diff-sha256", "b" * 64, "--fixture",
            ])
            self.assertEqual(code, 0)
            verdict = json.loads((out / "verdict.json").read_text(encoding="utf-8"))
            self.assertEqual(verdict["records"][0]["status"], "verified")


if __name__ == "__main__":
    unittest.main(verbosity=2)
