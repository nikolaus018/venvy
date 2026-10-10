"""Assembles the UI's HTML document.

web/index.html is the page skeleton; the markup of each view lives in its
own file under web/partials/ and is pulled in where a line reads

    <!-- include: partials/<name>.html -->

Includes are expanded here, on the server, so the browser still receives a
single HTML document and there is no build step. Deliberately stdlib-only:
emulator.py uses it too.
"""
import re
from pathlib import Path

_INCLUDE = re.compile(r"^[ \t]*<!-- include: ([A-Za-z0-9_][A-Za-z0-9_./-]*) -->[ \t]*$", re.M)
_MAX_DEPTH = 8


def render_index(web_dir: Path) -> str:
    """The complete page: web_dir/index.html with every include expanded."""
    root = web_dir.resolve()

    def load(rel: str, depth: int) -> str:
        if depth > _MAX_DEPTH:
            raise RuntimeError(f"UI includes nested too deeply at {rel}")
        path = (root / rel).resolve()
        if root not in path.parents:
            raise RuntimeError(f"UI include outside web/: {rel}")
        text = path.read_text(encoding="utf-8")
        # An include line is replaced by the file's content exactly as it is
        # (minus its final newline), so partials carry their own indentation.
        return _INCLUDE.sub(lambda m: load(m.group(1), depth + 1).rstrip("\n"), text)

    return load("index.html", 0)
