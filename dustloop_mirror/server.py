#!/usr/bin/env python3
"""Serves the dustloop mirror, translating wiki URLs to local file paths."""
import os, sys
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, unquote

class H(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        rel = unquote(urlparse(path).path).lstrip('/')
        base = os.getcwd()
        # Try, in order: exact, under site/, with .html, under site/ with .html
        for c in (rel, f"site/{rel}", f"{rel}.html", f"site/{rel}.html"):
            full = os.path.join(base, c)
            if os.path.isfile(full):
                return full
            if rel == '' and os.path.isdir(full):
                return full
        return os.path.join(base, rel)  # let it 404

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
print(f"Serving Dustloop mirror on port {port}")
ThreadingHTTPServer(('0.0.0.0', port), H).serve_forever()
