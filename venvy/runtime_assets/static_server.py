#!/usr/bin/env python3
"""Static file server behind Venvy's "Static site" runtime.

Started by Venvy as `python3 -I static_server.py --dir DIR --port N`. Plain
files are served as they are; what is added over `python -m http.server`:

  * no directory listings (a folder without index.html is a 404)
  * dotfiles and dot-folders are never served (except .well-known), and
    neither are the names given with --deny (Venvy passes `logs`)
  * clean URLs: /about serves about.html
  * a custom 404.html, and 200.html as the fallback for single-page apps
    (the convention of Surge and others): an unknown path serves it with 200
  * edits show up at once (Cache-Control: no-cache)
"""
import argparse
import http.server
import os
import sys
import urllib.parse


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--deny", default="", help="comma separated top-level names never served")
    args = ap.parse_args()

    root = os.path.realpath(args.dir)
    if not os.path.isdir(root):
        sys.exit(f"static site folder not found: {args.dir}")
    deny = {n for n in args.deny.split(",") if n}

    class Handler(http.server.SimpleHTTPRequestHandler):
        server_version = "VenvyStatic"

        def __init__(self, *a, **kw):
            super().__init__(*a, directory=root, **kw)

        def _hidden(self, url_path: str) -> bool:
            parts = [p for p in urllib.parse.unquote(url_path).split("/") if p]
            if any(p.startswith(".") and p != ".well-known" for p in parts):
                return True
            return bool(parts) and parts[0] in deny

        def _inside(self, fs_path: str) -> bool:
            real = os.path.realpath(fs_path)
            return real == root or real.startswith(root + os.sep)

        def _serve_page(self, name: str, status: int) -> None:
            """Send a file from the site root under *status* (404.html)."""
            with open(os.path.join(root, name), "rb") as f:
                body = f.read()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def send_head(self):
            url_path = urllib.parse.urlsplit(self.path).path
            if self._hidden(url_path):
                self.send_error(404)
                return None
            fs_path = self.translate_path(url_path)
            if not self._inside(fs_path):
                self.send_error(404)
                return None
            if not os.path.exists(fs_path) and not url_path.endswith("/"):
                if os.path.isfile(fs_path + ".html"):
                    self.path = url_path + ".html"
                    return super().send_head()
            if os.path.isdir(fs_path):
                # Index files only: the stock handler would list the folder.
                if not url_path.endswith("/"):
                    return super().send_head()      # its redirect to the slash form
                if not any(os.path.isfile(os.path.join(fs_path, i)) for i in ("index.html", "index.htm")):
                    self.send_error(404)
                    return None
                return super().send_head()
            if not os.path.exists(fs_path):
                if os.path.isfile(os.path.join(root, "200.html")):
                    self.path = "/200.html"
                    return super().send_head()
                if os.path.isfile(os.path.join(root, "404.html")):
                    self._serve_page("404.html", 404)
                    return None
                self.send_error(404)
                return None
            return super().send_head()

        def list_directory(self, path):
            self.send_error(404)
            return None

        def end_headers(self):
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            super().end_headers()

        def log_message(self, fmt, *a):
            sys.stdout.write("%s - %s\n" % (self.address_string(), fmt % a))
            sys.stdout.flush()

    class Server(http.server.ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    httpd = Server((args.host, args.port), Handler)
    print(f"Serving {root} on http://{args.host}:{args.port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
