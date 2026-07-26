#!/usr/bin/env python3
"""Rewrite relative Markdown links to absolute GitHub URLs for the PyPI page.

The committed ``README.md`` uses **relative** links and image paths so they
resolve nicely when browsing the repo on GitHub. PyPI, however, renders the
long description in isolation and cannot resolve relative links — they render
as dead text/broken images. This script rewrites every relative link/image to
an absolute URL pinned to the project's main branch:

* images (``![alt](path)``)  ->  raw.githubusercontent.com/<repo>/<branch>/path
* links  (``[text](path)``)  ->  github.com/<repo>/blob/<branch>/path

Absolute links (``http(s)://``, ``//``, ``mailto:``) and in-page anchors
(``#section``) are left untouched, so running it twice is a no-op.

Used by the PyPI publish workflow, which runs it against ``README.md`` in the
CI checkout right before ``python -m build`` — the repo copy stays relative.

Usage:
    python scripts/render_pypi_readme.py [README.md] [--output OUT]
"""
from __future__ import annotations

import argparse
import re
import sys

REPO = "ucr-bdlab/starlet"
BRANCH = "master"
RAW_BASE = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/"
BLOB_BASE = f"https://github.com/{REPO}/blob/{BRANCH}/"

# ![alt](target "optional title")  or  [text](target "optional title")
_LINK = re.compile(r'(!?)\[([^\]]*)\]\(\s*(<[^>]+>|[^)\s]+)((?:\s+"[^"]*")?)\s*\)')
_ABSOLUTE = ("http://", "https://", "//", "mailto:", "#")


def _rewrite_target(is_image: bool, target: str) -> str:
    stripped = target.strip("<>")
    if stripped.startswith(_ABSOLUTE):
        return target
    rel = stripped[2:] if stripped.startswith("./") else stripped
    base = RAW_BASE if is_image else BLOB_BASE
    return base + rel


def rewrite(markdown: str) -> tuple[str, int]:
    """Return (rewritten_markdown, num_links_rewritten), skipping code fences."""
    out_lines: list[str] = []
    count = 0
    in_fence = False
    fence_marker = ""

    for line in markdown.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            if not in_fence:
                in_fence, fence_marker = True, marker
            elif marker == fence_marker:
                in_fence, fence_marker = False, ""
            out_lines.append(line)
            continue
        if in_fence:
            out_lines.append(line)
            continue

        nonlocal_count = [count]

        def _sub(m: "re.Match[str]") -> str:
            bang, text, target, title = m.groups()
            new_target = _rewrite_target(bang == "!", target)
            if new_target != target.strip("<>"):
                nonlocal_count[0] += 1
            return f"{bang}[{text}]({new_target}{title})"

        out_lines.append(_LINK.sub(_sub, line))
        count = nonlocal_count[0]

    return "".join(out_lines), count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", default="README.md",
                        help="Markdown file to rewrite (default: README.md).")
    parser.add_argument("--output", "-o", default=None,
                        help="Output path (default: overwrite the input in place).")
    args = parser.parse_args(argv)

    with open(args.input, encoding="utf-8") as handle:
        source = handle.read()

    rewritten, count = rewrite(source)
    out_path = args.output or args.input
    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write(rewritten)

    print(f"render_pypi_readme: rewrote {count} relative link(s) -> absolute "
          f"({REPO}@{BRANCH}) in {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
