#!/usr/bin/env python3
"""Serves the dustloop mirror, translating wiki URLs to local file paths."""
import os, sys
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, unquote

LANDING_PAGE = '/w/Guilty_Gear_-Strive-'
BASE_DIR = os.path.realpath(os.path.dirname(os.path.abspath(__file__)))

# MIME types that indicate a failed guess (never valid for mirrored static files).
_USELESS_MIMES = frozenset({'application/x-httpd-php', 'application/octet-stream'})


class WikiRequestHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path in ('/', ''):
            self.send_response(302)
            self.send_header('Location', LANDING_PAGE)
            self.end_headers()
            return
        super().do_GET()

    def translate_path(self, path):
        parsed = urlparse(path)
        # MediaWiki's load.php and versioned asset URLs (fonts, icons) are saved
        # by wget with the full query string as part of the filename on disk, e.g.:
        #   load.php?lang=en&modules=skins.citizen.codex.styles&skin=citizen.css
        #   RobotoFlex_latin.woff2?d34c0
        # Build a "full" relative path that includes the query string.
        full_url = parsed.path + ('?' + parsed.query if parsed.query else '')
        full_rel = unquote(full_url).lstrip('/')
        path_rel = unquote(parsed.path).lstrip('/')
        base = BASE_DIR

        def safe(p):
            """Return realpath of p only if it stays within base, else None."""
            real = os.path.realpath(p)
            if real == base or real.startswith(base + os.sep):
                return real
            return None

        # 1. Try with the full URL (path+query) — this is the primary path for
        #    all load.php CSS/JS bundles and versioned font/icon files.
        if parsed.query:
            for c in (full_rel, f"site/{full_rel}",
                      f"{full_rel}.css", f"site/{full_rel}.css",
                      f"{full_rel}.js", f"site/{full_rel}.js"):
                full = safe(os.path.join(base, c))
                if full and os.path.isfile(full):
                    return full

        # 2. Standard path-only candidates (HTML pages, images, etc.)
        rel = path_rel
        for c in (rel, f"site/{rel}", f"{rel}.html", f"site/{rel}.html"):
            full = safe(os.path.join(base, c))
            if full is None:
                continue
            if os.path.isfile(full):
                return full
            if rel == '' and os.path.isdir(full):
                return full

        # 3. Fallback: for missing /wiki/images/X/XY/file.png requests,
        #    look in /wiki/images/thumb/X/XY/file.png/ and return the largest thumb.
        if 'wiki/images/' in rel and '/thumb/' not in rel:
            parts = rel.split('/')
            try:
                # rel looks like: wiki/images/X/XY/Filename.png
                # we want:       site/wiki/images/thumb/X/XY/Filename.png/
                idx = parts.index('images')
                thumb_dir = safe(os.path.join(
                    base, 'site', 'wiki', 'images', 'thumb',
                    *parts[idx+1:]
                ))
                if thumb_dir and os.path.isdir(thumb_dir):
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

        fallback = safe(os.path.join(base, rel))
        return fallback if fallback is not None else base

    def guess_type(self, path):
        # super().guess_type() returns a plain string (not a tuple).
        mime = super().guess_type(path)
        if mime and mime not in _USELESS_MIMES:
            return mime

        # Versioned files like RobotoFlex.woff2?d34c0 have the real extension
        # before the '?'. Strip the query suffix and retry.
        clean = path.partition('?')[0]
        if clean != path:
            mime = super().guess_type(clean)
            if mime and mime not in _USELESS_MIMES:
                return mime

        # Last resort: sniff the first bytes for SVG. The Citizen skin serves
        # its icons via load.php with no file extension, but the content is SVG.
        try:
            with open(path, 'rb') as f:
                header = f.read(256)
            snippet = header.lstrip()
            if snippet.startswith(b'<svg') or (
                snippet.startswith(b'<?xml') and b'<svg' in header
            ):
                return 'image/svg+xml'
        except OSError:
            pass

        return 'application/octet-stream'


port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
print(f"Serving Dustloop mirror on http://127.0.0.1:{port}")
ThreadingHTTPServer(('127.0.0.1', port), WikiRequestHandler).serve_forever()
