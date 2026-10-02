#!/usr/bin/env python3
"""Serves the dustloop mirror, translating wiki URLs to local file paths."""
import os, sys
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, unquote

LANDING_PAGE = '/w/Guilty_Gear_-Strive-'

TAB_FIX_CSS = b"""<style>
/* Override Tabber + Dustloop wrappers so all panels stack visibly.
   Includes parent containers (.tabber, .attack-gallery) that were
   clipping the inactive panel in the previous attempt. */
.tabber,
.tabber--init,
.tabber__section,
.tabbertab,
.attack-gallery {
    display: block !important;
    overflow: visible !important;
    height: auto !important;
    max-height: none !important;
    min-height: 0 !important;
    transform: none !important;
    position: static !important;
}
.tabber__panel,
.tabbertab,
[role="tabpanel"] {
    display: block !important;
    visibility: visible !important;
    width: auto !important;
    max-width: 100% !important;
    height: auto !important;
    transform: none !important;
    opacity: 1 !important;
    position: static !important;
    flex-shrink: 0 !important;
    margin-bottom: 1em;
}
.tabber__header,
.tabber__tabs {
    display: none !important;
}
</style>
"""

def _expand_modules(spec):
    """Expand MediaWiki's packed module list: 'a.b,c|d' -> {'a.b','a.c','d'}."""
    out = set()
    for group in spec.split('|'):
        parts = group.split(',')
        head = parts[0]
        out.add(head)
        prefix = head.rsplit('.', 1)[0] + '.' if '.' in head else ''
        for p in parts[1:]:
            out.add(prefix + p)
    return {m for m in out if m}


def _modules_from(query):
    """Pull the module set out of a load.php query (tolerates truncated names)."""
    if 'modules=' not in query:
        return set()
    spec = query.split('modules=', 1)[1].split('&', 1)[0]
    return _expand_modules(spec)


def closest_load_php(base, rel, query):
    """wget truncates long load.php filenames and pages ask for module combos
    that were never saved. Pick the saved stylesheet that covers the most of
    the requested modules instead of 404ing."""
    if not rel.endswith('load.php') or 'only=styles' not in query:
        return None
    wanted = _modules_from(query)
    if not wanted:
        return None
    best, best_score = None, (0, 0)
    for d in (os.path.join(base, 'site', os.path.dirname(rel)),
              os.path.join(base, os.path.dirname(rel))):
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if not name.startswith('load.php?') or 'only=scripts' in name:
                continue
            have = _modules_from(name[len('load.php?'):])
            score = (len(wanted & have), -len(have - wanted))
            if score > best_score:
                best, best_score = os.path.join(d, name), score
    return best


class H(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path in ('/', ''):
            self.send_response(302)
            self.send_header('Location', LANDING_PAGE)
            self.end_headers()
            return

        translated = self.translate_path(self.path)
        if translated.endswith('.html') and os.path.isfile(translated):
            try:
                with open(translated, 'rb') as f:
                    body = f.read()
                if b'</head>' in body:
                    body = body.replace(b'</head>', TAB_FIX_CSS + b'</head>', 1)
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            except (OSError, IOError):
                pass

        super().do_GET()

    def translate_path(self, path):
        parsed = urlparse(path)
        rel = unquote(parsed.path).lstrip('/')
        query = unquote(parsed.query)  # decode %7C → |, %2C → , etc.
        base = os.getcwd()

        # Build candidates. When there's a query string, wget often saved
        # the file with the query as part of the filename, sometimes with
        # an extension appended (.css, .html) by --adjust-extension.
        candidates = []
        if query:
            rel_q = f"{rel}?{query}"
            for ext in ('', '.css', '.html', '.js'):
                candidates.append(f"{rel_q}{ext}")
                candidates.append(f"site/{rel_q}{ext}")

        # Then standard candidates without query string
        for ext in ('', '.html'):
            candidates.append(f"{rel}{ext}")
            candidates.append(f"site/{rel}{ext}")

        for c in candidates:
            full = os.path.join(base, c)
            if os.path.isfile(full):
                return full
            if rel == '' and os.path.isdir(full):
                return full

        # Fallback: no exact load.php match -> closest stylesheet bundle
        if query:
            match = closest_load_php(base, rel, query)
            if match:
                return match

        # Fallback: missing /wiki/images/X/XY/file.png → largest thumbnail
        if 'wiki/images/' in rel and '/thumb/' not in rel:
            parts = rel.split('/')
            try:
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
                    cands = [n for n in os.listdir(thumb_dir) if 'px-' in n]
                    if cands:
                        return os.path.join(thumb_dir, max(cands, key=size_key))
            except (ValueError, OSError):
                pass

        return os.path.join(base, rel)

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
print(f"Serving Dustloop mirror on port {port}")
ThreadingHTTPServer(('0.0.0.0', port), H).serve_forever()