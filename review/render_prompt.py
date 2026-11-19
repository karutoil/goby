#!/usr/bin/env python3
"""Render the reviewer's brief: inline the diff while it fits, point at it when it does not.

The whole diff is inlined into the prompt. That is the right shape for a normal change and
the wrong one for a large pull request: an 18k-line diff is roughly 195k tokens of prompt,
re-sent on every turn, and the agent reaches the end of its context before it reaches the
end of the diff. So past `GOBY_INLINE_DIFF_LINES` the brief names the file instead, with a
manifest of the largest changes. The agent has a shell and the same bytes: `grep`, `sed`
and the sandbox are all it needs to read a file that does not fit in one message.

The decision is here rather than in the shell so it can be tested: given a diff, a numstat
and a limit, what the reviewer is told is a value, not a side effect of a heredoc.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Iterable, Mapping, Sequence

#: Files listed in a manifest. Enough to see the shape of the change, not the whole tree.
MANIFEST_ROWS = 30
#: A path longer than this is truncated in the manifest, not dropped.
MAX_PATH = 120


def _rows(numstat: str) -> list[tuple[int, int, str]]:
    """`git diff --numstat` → (added, removed, path), binary rows counted as 0/0."""
    rows: list[tuple[int, int, str]] = []
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, removed, path = parts
        if not path.strip():
            continue
        try:
            counts = (int(added), int(removed))
        except ValueError:
            counts = (0, 0)  # `-` and `-`: a binary file changes without lines
        rows.append((counts[0], counts[1], path.strip()))
    return rows


def _manifest(diff_lines: int, numstat: str, *, diff_path: str, sandbox: str) -> str:
    rows = _rows(numstat)
    total = len(rows)
    largest = sorted(rows, key=lambda row: (-(row[0] + row[1]), row[2]))[:MANIFEST_ROWS]
    listed = "\n".join(
        f"  {added:>6} +  {removed:>6} -  {path if len(path) <= MAX_PATH else path[:MAX_PATH] + '…'}"
        for added, removed, path in largest
    )
    rest = total - len(largest)
    lines = [
        f"The diff is **{diff_lines:,} lines across {total} files** — too large to inline. It is the",
        f"same bytes the harness took, at `{diff_path}`; the repository at this commit is",
        f"`{sandbox}`. Read the change there, a file at a time.",
        "",
        "Largest changes (added / removed / path):",
        "",
        "```",
        listed if listed else "  (empty diff)",
        "```",
    ]
    if rest > 0:
        lines += ["", f"{rest} more file(s): `grep -n '^diff --git' {diff_path}`"]
    lines += [
        "",
        "Read it with `grep -n '^diff --git'`, `sed -n '1,200p'`, or by opening the file in the",
        "sandbox. Do not guess at a change you have not read: a finding you cannot reproduce",
        "is withheld, and the diff being large is not an excuse for a comment you cannot prove.",
    ]
    return "\n".join(lines)


def diff_block(diff: str, numstat: str, *, inline_lines: int, diff_path: str = "{{OUT}}/diff.patch",
               sandbox: str = "{{SANDBOX}}") -> str:
    """The `{{DIFF}}` value: a fenced diff, or a manifest that says where the diff is."""
    if inline_lines < 0:
        raise ValueError("inline_lines must be non-negative")
    lines = len(diff.splitlines())
    if lines <= inline_lines:
        body = diff.rstrip("\n")
        return f"```diff\n{body}\n```" if body else "```diff\n```"
    return _manifest(lines, numstat, diff_path=diff_path, sandbox=sandbox)


def render(template: str, values: Mapping[str, str], diff: str, numstat: str, *,
           inline_lines: int) -> str:
    """Substitute every `{{NAME}}`, including `{{DIFF}}`.

    `{{OUT}}` and `{{SANDBOX}}` are substituted into the manifest too, so the paths the agent
    is told to read are the paths it can actually reach.
    """
    rendered = template
    for name, value in values.items():
        rendered = rendered.replace(f"{{{{{name}}}}}", value)
    block = diff_block(
        diff,
        numstat,
        inline_lines=inline_lines,
        diff_path=f"{values.get('OUT', '{{OUT}}')}/diff.patch",
        sandbox=values.get("SANDBOX", "{{SANDBOX}}"),
    )
    rendered = rendered.replace("{{DIFF}}", block)
    return rendered


def unresolved(text: str) -> list[str]:
    """Any `{{NAME}}` left after rendering: a placeholder nobody filled is a silent instruction
    the reviewer never receives."""
    out: list[str] = []
    index = 0
    while True:
        start = text.find("{{", index)
        if start < 0:
            return out
        end = text.find("}}", start + 2)
        if end < 0:
            return out
        name = text[start + 2:end]
        if name and name.replace("_", "").isalnum():
            out.append(name)
        index = end + 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render the reviewer's brief")
    parser.add_argument("--template", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--diff", required=True, type=Path)
    parser.add_argument("--numstat", required=True, type=Path)
    parser.add_argument("--inline-lines", type=int, default=4000)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--sandbox", required=True)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args(argv)

    template = args.template.read_text(encoding="utf-8")
    diff = args.diff.read_text(encoding="utf-8", errors="replace")
    numstat = args.numstat.read_text(encoding="utf-8", errors="replace") if args.numstat.exists() else ""
    values = {
        "REPO": args.repo,
        "HEAD": args.head,
        "BASE": args.base,
        "SANDBOX": args.sandbox,
        "REFERENCE": args.reference,
        "OUT": args.out_dir,
    }
    # Check the template, never the rendered text: a source file in the diff may legitimately
    # contain `{{...}}`, and a guard that scans the output would refuse those reviews.
    provided = set(values) | {"DIFF"}
    left = [name for name in unresolved(template) if name not in provided]
    if left:
        print(f"render_prompt: unresolved placeholder(s): {', '.join(left)}", file=sys.stderr)
        return 2
    if "{{DIFF}}" not in template:
        print("render_prompt: the template has no {{DIFF}} block", file=sys.stderr)
        return 2
    rendered = render(template, values, diff, numstat, inline_lines=args.inline_lines)
    args.out.write_text(rendered, encoding="utf-8")
    print(json.dumps({
        "diff_lines": len(diff.splitlines()),
        "inlined": len(diff.splitlines()) <= args.inline_lines,
        "chars": len(rendered),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
