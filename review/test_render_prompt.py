#!/usr/bin/env python3
"""Tests for the brief renderer: inline while it fits, manifest when it does not."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import render_prompt as renderer  # noqa: E402

TEMPLATE = """# Review {{REPO}} at {{HEAD}} against {{BASE}}
sandbox={{SANDBOX}} reference={{REFERENCE}} out={{OUT}}

## The diff

{{DIFF}}

Call report_findings once.
"""

VALUES = {
    "REPO": "owner/name",
    "HEAD": "a" * 40,
    "BASE": "b" * 40,
    "SANDBOX": "/workspace/sandbox",
    "REFERENCE": "/workspace/repo",
    "OUT": "/workspace/out",
}

DIFF = (
    "diff --git a/src/a.ts b/src/a.ts\n"
    "--- a/src/a.ts\n"
    "+++ b/src/a.ts\n"
    "@@ -1,2 +1,3 @@\n"
    " one\n"
    "+two\n"
    " three\n"
)

NUMSTAT = "2\t0\tsrc/a.ts\n"


def render(template: str = TEMPLATE, diff: str = DIFF, numstat: str = NUMSTAT, inline_lines: int = 100) -> str:
    return renderer.render(template, VALUES, diff, numstat, inline_lines=inline_lines)


class InlineTests(unittest.TestCase):
    def test_a_small_diff_is_fenced_in_the_prompt(self):
        text = render()
        self.assertIn("```diff\n", text)
        self.assertIn("+two", text)
        self.assertNotIn("too large to inline", text)
        self.assertEqual(renderer.unresolved(text), [])

    def test_the_placeholder_values_are_substituted(self):
        text = render()
        self.assertIn("# Review owner/name at " + "a" * 40, text)
        self.assertIn("sandbox=/workspace/sandbox", text)
        self.assertIn("out=/workspace/out", text)

    def test_a_diff_exactly_at_the_limit_still_inlines(self):
        lines = "\n".join(f"+ line {i}" for i in range(5))
        text = render(diff=lines + "\n", inline_lines=5)
        self.assertIn("+ line 4", text)
        self.assertNotIn("too large", text)

    def test_one_line_over_the_limit_does_not(self):
        lines = "\n".join(f"+ line {i}" for i in range(6))
        text = render(diff=lines + "\n", inline_lines=5)
        self.assertNotIn("+ line 5", text)
        self.assertIn("too large to inline", text)

    def test_an_empty_diff_is_an_empty_fence(self):
        self.assertEqual(renderer.diff_block("", "", inline_lines=10), "```diff\n```")


class ManifestTests(unittest.TestCase):
    def test_the_manifest_names_the_file_the_tree_and_the_totals(self):
        big = "\n".join(f"+ line {i}" for i in range(40)) + "\n"
        numstat = "1200\t300\tweb/src/app.tsx\n7\t2\tweb/src/x.css\n"
        text = render(diff=big, numstat=numstat, inline_lines=10)
        self.assertIn("40 lines across 2 files", text)
        self.assertIn("/workspace/out/diff.patch", text)
        self.assertIn("/workspace/sandbox", text)
        self.assertIn("web/src/app.tsx", text)
        self.assertIn("1200", text)

    def test_the_manifest_points_at_one_file_diffs_when_it_knows_the_base(self):
        big = "\n".join(f"+ line {i}" for i in range(40)) + "\n"
        text = render(diff=big, numstat="40\t0\ta.ts\n", inline_lines=10)
        self.assertIn("git -C /workspace/sandbox diff --stat " + "b" * 40, text)
        self.assertIn("diff " + "b" * 40 + " -- <path>", text)

    def test_the_manifest_does_not_leak_the_diff_body(self):
        big = "\n".join(f"+ secret-line-{i}" for i in range(40)) + "\n"
        text = render(diff=big, numstat="40\t0\ta.ts\n", inline_lines=10)
        self.assertNotIn("secret-line-39", text)

    def test_files_beyond_the_row_cap_are_counted_not_listed(self):
        rows = "\n".join(f"1\t0\tfile{i}.ts" for i in range(renderer.MANIFEST_ROWS + 5))
        big = "\n".join("+ x" for _ in range(50)) + "\n"
        text = render(diff=big, numstat=rows, inline_lines=10)
        self.assertIn("5 more file(s)", text)
        self.assertIn("grep -n '^diff --git'", text)

    def test_a_binary_row_does_not_crash_and_is_listed(self):
        text = renderer.diff_block("\n".join("+x" for _ in range(20)) + "\n", "-\t-\tassets/logo.png\n",
                                   inline_lines=5)
        self.assertIn("assets/logo.png", text)
        self.assertIn("0 +", text)


class GuardTests(unittest.TestCase):
    def test_an_unfilled_placeholder_is_reported(self):
        self.assertEqual(renderer.unresolved("a {{MISSING}} b"), ["MISSING"])
        self.assertEqual(renderer.unresolved("a {not a placeholder} b"), [])
        self.assertEqual(renderer.unresolved("a {{CLOSED"), [])

    def test_a_negative_limit_is_refused(self):
        with self.assertRaises(ValueError):
            renderer.diff_block("x", "", inline_lines=-1)


class CommandLineTests(unittest.TestCase):
    def write(self, root: Path, name: str, text: str) -> Path:
        path = root / name
        path.write_text(text, encoding="utf-8")
        return path

    def run_cli(self, root: Path, template: str, diff: str, numstat: str, inline: int = 100) -> tuple[int, Path]:
        out = root / "prompt.md"
        code = renderer.main([
            "--template", str(self.write(root, "t.md", template)),
            "--out", str(out),
            "--diff", str(self.write(root, "d.patch", diff)),
            "--numstat", str(self.write(root, "n.txt", numstat)),
            "--inline-lines", str(inline),
            "--repo", VALUES["REPO"], "--head", VALUES["HEAD"], "--base", VALUES["BASE"],
            "--sandbox", VALUES["SANDBOX"], "--reference", VALUES["REFERENCE"], "--out-dir", VALUES["OUT"],
        ])
        return code, out

    def test_it_writes_the_rendered_brief(self):
        with tempfile.TemporaryDirectory() as temporary:
            code, out = self.run_cli(Path(temporary), TEMPLATE, DIFF, NUMSTAT)
            self.assertEqual(code, 0)
            self.assertIn("+two", out.read_text(encoding="utf-8"))

    def test_an_unresolved_placeholder_fails_instead_of_shipping(self):
        with tempfile.TemporaryDirectory() as temporary:
            code, out = self.run_cli(Path(temporary), "{{REPO}} {{MYSTERY}}\n{{DIFF}}", DIFF, NUMSTAT)
            self.assertEqual(code, 2)
            self.assertFalse(out.exists())

    def test_a_diff_that_contains_braces_is_not_an_unresolved_placeholder(self):
        with tempfile.TemporaryDirectory() as temporary:
            code, out = self.run_cli(Path(temporary), TEMPLATE, "+ const x = {{VALUE}};\n", "1\t0\ta.ts\n")
            self.assertEqual(code, 0)
            self.assertIn("{{VALUE}}", out.read_text(encoding="utf-8"))

    def test_it_reports_what_it_did(self):
        import contextlib, io
        with tempfile.TemporaryDirectory() as temporary:
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code, out = self.run_cli(Path(temporary), TEMPLATE, DIFF, NUMSTAT, inline=100)
            self.assertEqual(code, 0)
            self.assertTrue(out.exists())
            payload = json.loads(buffer.getvalue())
            self.assertTrue(payload["inlined"])
            self.assertEqual(payload["diff_lines"], 7)


if __name__ == "__main__":
    unittest.main(verbosity=2)
