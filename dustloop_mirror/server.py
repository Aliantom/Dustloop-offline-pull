#!/usr/bin/env python3
"""Serves the dustloop mirror, translating wiki URLs to local file paths."""
import html, os, re, sys
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
/* Frame Data details rows (expanded from data-mw-details by the mirror) */
.mirror-details > td { background: rgba(127,127,127,0.07); text-align: left; }
.mirror-details summary { cursor: pointer; font-weight: 600; padding: 0.2em 0; }
.mirror-details table { margin: 0.3em 0; }
.mirror-details figure { display: inline-block; margin: 0.3em; vertical-align: top; }
/* Combo videos downloaded into the mirror */
.mirror-video { margin: 0.5em 0; max-width: 100%; }
.mirror-video video { width: 100%; max-width: 720px; display: block; background: #000; }
.mirror-video figcaption { font-size: 0.9em; opacity: 0.8; }
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


SCRIPT_RE = re.compile(rb"<script\b[^>]*>.*?</script\s*>", re.I | re.S)

# Keep in sync with YOUTUBE_ID_RE in dustloop_mirror.py.
YOUTUBE_ID_RE = re.compile(
    r"(?:youtube(?:-nocookie)?\.com/(?:embed/|watch\?v=|shorts/|v/)|youtu\.be/|ytimg\.com/vi/)"
    r"([A-Za-z0-9_-]{11})")
EMBED_FIGURE_RE = re.compile(
    r"<figure\b[^>]*class=\"[^\"]*embedvideo[^\"]*\"[^>]*>.*?</figure>", re.I | re.S)
IFRAME_RE = re.compile(r"<iframe\b[^>]*>.*?</iframe>", re.I | re.S)
FIGCAPTION_RE = re.compile(r"<figcaption\b.*?</figcaption>", re.I | re.S)
YT_LINK_RE = re.compile(
    r"href=\"(https?:)?//(?:www\.)?(?:youtube\.com|youtu\.be)/[^\"]*\"", re.I)
YT_TIME_RE = re.compile(r"[?&](?:amp;)?t=(\d+)")
YT_DIR = os.path.join("site", "_media", "yt")


def local_video(vid):
    rel = f"{YT_DIR}/{vid}.mp4"
    return "/" + rel[len("site/"):] if os.path.isfile(os.path.join(os.getcwd(), rel)) else None


def video_tag(src, caption=""):
    return (f'<figure class="mirror-video"><video controls preload="metadata" '
            f'src="{src}"></video>{caption}</figure>')


def swap_youtube(body):
    """Replace YouTube embeds and links with videos downloaded into the mirror.
    Embeds whose video hasn't been downloaded are left alone."""
    def fig(m):
        block = m.group(0)
        ids = YOUTUBE_ID_RE.findall(html.unescape(block))
        src = local_video(ids[0]) if ids else None
        if not src:
            return block
        cap = FIGCAPTION_RE.search(block)
        return video_tag(src, cap.group(0) if cap else "")

    def frame(m):
        ids = YOUTUBE_ID_RE.findall(html.unescape(m.group(0)))
        src = local_video(ids[0]) if ids else None
        return video_tag(src) if src else m.group(0)

    def link(m):
        url = html.unescape(m.group(0))
        ids = YOUTUBE_ID_RE.findall(url)
        src = local_video(ids[0]) if ids else None
        if not src:
            return m.group(0)
        t = YT_TIME_RE.search(url)
        return f'href="{src}' + (f"#t={t.group(1)}" if t else "") + '"'

    body = EMBED_FIGURE_RE.sub(fig, body)
    body = IFRAME_RE.sub(frame, body)
    return YT_LINK_RE.sub(link, body)


def status_page():
    path = os.path.join(os.getcwd(), "status.md")
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        text = "# No status yet\n\nThis mirror was downloaded before status reports existed."
    out, in_list = [], False
    for line in text.splitlines():
        esc = html.escape(line)
        esc = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc)
        if line.startswith("- "):
            if not in_list:
                out.append("<ul>"); in_list = True
            item = esc[2:]
            m = re.match(r"(GGST/[^:]+|Guilty Gear -Strive-)(.*)", line[2:])
            if m:
                href = "/w/" + m.group(1).replace(" ", "_")
                item = f'<a href="{html.escape(href)}">{html.escape(m.group(1))}</a>{html.escape(m.group(2))}'
            out.append(f"<li>{item}</li>")
            continue
        if in_list:
            out.append("</ul>"); in_list = False
        if line.startswith("# "):
            out.append(f"<h1>{esc[2:]}</h1>")
        elif line.strip():
            out.append(f"<p>{esc}</p>")
    if in_list:
        out.append("</ul>")
    return ("<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width'>"
            "<title>Mirror status</title><style>"
            "body{font-family:system-ui,sans-serif;max-width:52rem;margin:2rem auto;"
            "padding:0 1rem;line-height:1.5;color:#222;background:#fafafa}"
            "h1{font-size:1.5rem}li{margin:.15rem 0}a{color:#b0002a}"
            "</style></head><body>" + "\n".join(out) +
            "<p><a href='/w/Guilty_Gear_-Strive-'>Go to the GGST wiki</a></p>"
            "</body></html>").encode("utf-8")


class H(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path in ('/', ''):
            self.send_response(302)
            self.send_header('Location', LANDING_PAGE)
            self.end_headers()
            return

        if self.path.split("?", 1)[0].rstrip("/") == "/_status":
            body = status_page()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        translated = self.translate_path(self.path)
        if translated.endswith('.html') and os.path.isfile(translated):
            try:
                with open(translated, 'rb') as f:
                    body = f.read()
                # Pages from the old crawler still carry live-site scripts
                # (e.g. Cloudflare's bot check); they can't work offline.
                body = SCRIPT_RE.sub(b'', body)
                if b'youtu' in body or b'ytimg' in body:
                    body = swap_youtube(body.decode('utf-8', 'replace')).encode('utf-8')
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