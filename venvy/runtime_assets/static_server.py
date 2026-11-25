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
import threading
import urllib.parse

_REQUEST_TIMEOUT = 15.0
_MAX_CONCURRENT = 64
_MAX_ERROR_PAGE_BYTES = 2 * 1024 * 1024


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--root", default="", help="app root that --dir must stay inside")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--deny", default="", help="comma separated top-level names never served")
    args = ap.parse_args()

    if args.root:
        app_root = os.path.realpath(args.root)
        cand = args.dir
        if os.path.islink(cand):
            sys.exit(f"static site folder must not be a symlink: {args.dir}")
        root = os.path.realpath(cand)
        if not (root == app_root or root.startswith(app_root + os.sep)):
            sys.exit(f"static site folder escapes app directory: {args.dir}")
    else:
        root = os.path.realpath(args.dir)
    if not os.path.isdir(root):
        sys.exit(f"static site folder not found: {args.dir}")
    deny = {n.lower() for n in args.deny.split(",") if n}
    conn_sem = threading.BoundedSemaphore(_MAX_CONCURRENT)

    class Handler(http.server.SimpleHTTPRequestHandler):
        server_version = "VenvyStatic"
        timeout = _REQUEST_TIMEOUT

        def __init__(self, *a, **kw):
            super().__init__(*a, directory=root, **kw)

        def setup(self):
            self.request.settimeout(_REQUEST_TIMEOUT)
            super().setup()

        def _parts_hidden(self, parts) -> bool:
            if any(p.startswith(".") and p != ".well-known" for p in parts):
                return True
            return bool(parts) and parts[0].lower() in deny

        def _hidden(self, url_path: str) -> bool:
            parts = [p for p in urllib.parse.unquote(url_path).split("/") if p]
            return self._parts_hidden(parts)

        def _inside(self, fs_path: str) -> bool:
            real = os.path.realpath(fs_path)
            if not (real == root or real.startswith(root + os.sep)):
                return False
            if real != root:
                rel = os.path.relpath(real, root)
                rel_parts = [p for p in rel.split(os.sep) if p and p != "."]
                if self._parts_hidden(rel_parts):
                    return False
            return True

        def _serve_page(self, name: str, status: int) -> None:
            """Send a file from the site root under *status* (404.html)."""
            with open(os.path.join(root, name), "rb") as f:
                body = f.read(_MAX_ERROR_PAGE_BYTES)
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _send_fallback_or_404(self):
            p200 = os.path.join(root, "200.html")
            if os.path.isfile(p200) and self._inside(p200):
                self.path = "/200.html"
                return super().send_head()
            p404 = os.path.join(root, "404.html")
            if os.path.isfile(p404) and self._inside(p404):
                self._serve_page("404.html", 404)
                return None
            self.send_error(404)
            return None

        def send_head(self):
            parts = urllib.parse.urlsplit(self.path)
            if parts.netloc or self.path.startswith("//"):
                self.send_error(404)
                return None
            url_path = parts.path
            if self._hidden(url_path):
                self.send_error(404)
                return None
            fs_path = self.translate_path(url_path)
            if not self._inside(fs_path):
                self.send_error(404)
                return None
            if not os.path.exists(fs_path) and not url_path.endswith("/"):
                html_path = fs_path + ".html"
                if os.path.isfile(html_path) and self._inside(html_path):
                    self.path = url_path + ".html"
                    return super().send_head()
            if os.path.isdir(fs_path):
                # Index files only: the stock handler would list the folder.
                index_ok = False
                for i in ("index.html", "index.htm"):
                    cand = os.path.join(fs_path, i)
                    if os.path.isfile(cand):
                        index_ok = self._inside(cand)
                        break
                if not index_ok:
                    return self._send_fallback_or_404()
                if not url_path.endswith("/"):
                    return super().send_head()      # redirect to the slash form only when index exists
                return super().send_head()
            if not os.path.exists(fs_path):
                return self._send_fallback_or_404()
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

        def process_request(self, request, client_address):
            if not conn_sem.acquire(blocking=False):
                try:
                    request.close()
                except OSError:
                    pass
                return
            try:
                super().process_request(request, client_address)
            except Exception:
                conn_sem.release()
                raise

        def process_request_thread(self, request, client_address):
            try:
                super().process_request_thread(request, client_address)
            finally:
                conn_sem.release()

    httpd = Server((args.host, args.port), Handler)
    print(f"Serving {root} on http://{args.host}:{args.port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
