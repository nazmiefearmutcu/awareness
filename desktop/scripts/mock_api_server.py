#!/usr/bin/env python3
"""Minimal loopback mock of awareness-api for desktop shell e2e tests."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer

HOST = "127.0.0.1"
PORT = 8085


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/healthz"):
            body = b'{"ok":true,"service":"mock-awareness-api"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        html = b"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Awareness mock</title>
<style>
:root{color-scheme:dark}
body{margin:0;font-family:system-ui,Segoe UI,sans-serif;background:#0c0c0e;color:#e8e8ea}
header{padding:12px 20px;border-bottom:1px solid #2a2a30;display:flex;gap:16px;align-items:center}
nav a{color:#8b8b93;margin-right:12px;text-decoration:none}
nav a.active{color:#e8e8ea}
main{padding:24px}
.card{border:1px solid #2a2a30;border-radius:10px;padding:16px;max-width:480px}
</style></head><body>
<header><strong>Awareness</strong>
<nav>
<a class="active" href="#">Dashboard</a>
<a href="#">Captures</a>
<a href="#">Work</a>
<a href="#">Settings</a>
</nav></header>
<main><div class="card">
<h1 style="font-size:1.1rem;margin:0 0 8px">Mock dashboard</h1>
<p style="color:#8b8b93;margin:0">Desktop shell attached successfully (e2e mock API).</p>
</div></main>
</body></html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


def main() -> None:
    HTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
