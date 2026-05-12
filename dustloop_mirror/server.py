#!/usr/bin/env python3
"""Serves the dustloop mirror, translating wiki URLs to local file paths."""
import os, sys
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, unquote

LANDING_PAGE = '/w/Guilty_Gear_-Strive-'

class H(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path in ('/', ''):
            self.send_response(302)
            self.send_header('Location', LANDING_PAGE)
            self.end_headers()
            return
        super().do_GET()

    def translate_path(self, path):
        rel = unquote(urlparse(path).path).lstrip('/')
        base = os.getcwd()

        # Standard candidates
        for c in (rel, f"site/{rel}", f"{rel}.html", f"site/{rel}.html"):
            full = os.path.join(base, c)
            if os.path.isfile(full):
                return full
            if rel == '' and os.path.isdir(full):
                return full

        # Fallback: for missing /wiki/images/X/XY/file.png requests,
        # look in /wiki/images/thumb/X/XY/file.png/ and return the largest thumb.
        if 'wiki/images/' in rel and '/thumb/' not in rel:
            parts = rel.split('/')
            try:
                # rel looks like: wiki/images/X/XY/Filename.png
                # we want:       site/wiki/images/thumb/X/XY/Filename.png/
                idx = parts.index('images')
                thumb_dir = os.path.join(
                    base, 'site', 'wiki', 'images', 'thumb',
                    *parts[idx+1:]
                )
                if os.path.isdir(thumb_dir):
                    def size_key(name):
                        try:
                            return int(name.split('px-')[0])
                        except (ValueError, IndexError):
                            return 0
                    candidates = [n for n in os.listdir(thumb_dir) if 'px-' in n]
                    if candidates:
                        return os.path.join(thumb_dir, max(candidates, key=size_key))
            except (ValueError, OSError):
                pass

        return os.path.join(base, rel)

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
print(f"Serving Dustloop mirror on port {port}")
ThreadingHTTPServer(('0.0.0.0', port), H).serve_forever()