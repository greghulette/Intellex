"""Render the downloaded wikis as pages this app can serve with no network.

The wikis are plain markdown in git repos, so the only real work is turning them
into HTML and making their links point at us instead of at github.com. Rendered
ON REQUEST rather than at download time: pages are small, it keeps the fetcher to
one job, and it means a wiki dropped into the directory by hand works with no
build step.

THE LINKS ARE THE WHOLE PROBLEM. Left alone, every one of them leaves the app for
github.com -- which is exactly the moment there is no internet, and the reason
this feature exists. Two dialects have to be caught because the wikis do not
agree with each other: NaviCore and Intellex write `[[Page Name]]`, the WCB wiki
writes `[Text](Page-Name)`. Rewriting is done in markdown-it's RENDERER RULES
rather than by regex over the finished HTML, so a URL inside a code block or an
example is left alone -- which a regex pass would not do, and those wikis are
full of command examples.

IMAGES THAT WERE TOO BIG TO DOWNLOAD become a link to the online copy, not a
broken picture. tools/fetch_wiki.py leaves anything over 1 MB behind (the WCB
wiki's assembly photographs are 6-7 MB each), so this is a normal state and not
an error worth shouting about.
"""
from __future__ import annotations

import html
import pathlib
import re
import urllib.parse

import paths

# owner/repo per product, for the "view this online" links. Kept in step with
# tools/fetch_wiki.py's WIKIS -- imported from there when it can be, because a
# second copy of a fact is a second thing to forget.
try:
    from fetch_wiki import TITLES, WIKIS
except Exception:                                        # noqa: BLE001
    WIKIS = {"intellex": ("greghulette", "Intellex"),
             "navicore": ("greghulette", "NaviCore"),
             "wcb": ("greghulette", "Wireless_Communication_Board-WCB")}
    TITLES = {"intellex": "Intellex", "navicore": "NaviCore", "wcb": "WCB"}

# Pages GitHub treats as furniture rather than content.
FURNITURE = {"_Sidebar", "_Footer", "_Header"}

_WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")


def root(product: str) -> pathlib.Path:
    return paths.data_dir("wiki") / product


def available() -> list[dict]:
    """Which wikis are on disk, for the picker and the launcher."""
    out = []
    for product in WIKIS:
        d = root(product)
        pages = sorted(p.stem for p in d.glob("*.md")) if d.is_dir() else []
        out.append({
            "product": product,
            "title": TITLES.get(product, product),
            "pages": [p for p in pages if p not in FURNITURE],
            "count": len([p for p in pages if p not in FURNITURE]),
            "have": bool(pages),
        })
    return out


def _page_file(product: str, name: str) -> pathlib.Path | None:
    """Resolve a page name to a file, refusing anything outside the wiki dir."""
    base = root(product).resolve()
    # Wiki page names are flat and use "-" for spaces; a slash or ".." in one is
    # not a page, it is an attempt to leave the directory.
    safe = urllib.parse.unquote(name).replace(" ", "-")
    if not safe or "/" in safe or "\\" in safe or ".." in safe:
        return None
    f = (base / f"{safe}.md")
    try:
        if f.resolve().parent != base or not f.is_file():
            return None
    except OSError:
        return None
    return f


def _wikilinks_to_md(text: str) -> str:
    """[[Target]] and [[Text|Target]] -> ordinary markdown links.

    Done before parsing so the renderer rules below see ONE kind of link. The
    pipe order is GitHub's: display text first, target second.
    """
    def one(m: re.Match) -> str:
        inner = m.group(1)
        if "|" in inner:
            shown, target = inner.split("|", 1)
        else:
            shown = target = inner
        return f"[{shown.strip()}]({target.strip().replace(' ', '-')})"
    return _WIKILINK.sub(one, text)


def _make_md(product: str):
    """A markdown renderer whose links and images point back at this app."""
    from markdown_it import MarkdownIt

    owner, repo = WIKIS.get(product, ("greghulette", product))
    raw_base = f"https://raw.githubusercontent.com/wiki/{owner}/{repo}"
    base = root(product)

    md = MarkdownIt("commonmark").enable(["table", "strikethrough"])

    def resolve_href(href: str) -> str:
        if href.startswith(("http://", "https://", "mailto:", "#", "/")):
            return href
        anchor = ""
        if "#" in href:
            href, anchor = href.split("#", 1)
            anchor = "#" + anchor
        if not href:
            return anchor or "#"
        # A file extension means an asset; anything else is a sibling page.
        if pathlib.PurePosixPath(href).suffix.lower() in (
                ".md", ".markdown", ""):
            page = href.strip("/").replace(" ", "-")
            page = page.rsplit(".", 1)[0] if page.lower().endswith(".md") else page
            return f"/wiki/{product}/{urllib.parse.quote(page)}{anchor}"
        return f"/wiki/{product}/{urllib.parse.quote(href.lstrip('./'))}{anchor}"

    def link_open(self, tokens, idx, options, env):
        t = tokens[idx]
        href = t.attrGet("href") or ""
        t.attrSet("href", resolve_href(href))
        if href.startswith(("http://", "https://")):
            # Opening the real browser for an external link keeps the app window
            # on the docs; target is meaningless in a webview otherwise.
            t.attrSet("target", "_blank")
            t.attrSet("rel", "noopener")
        return self.renderToken(tokens, idx, options, env)

    def image(self, tokens, idx, options, env):
        t = tokens[idx]
        src = t.attrGet("src") or ""
        alt = t.content or ""
        if src.startswith(("http://", "https://")):
            return f'<img src="{html.escape(src)}" alt="{html.escape(alt)}">'
        rel = src.lstrip("./").split("#", 1)[0]
        local = base / rel
        if local.is_file():
            return (f'<img src="/wiki/{product}/{urllib.parse.quote(rel)}" '
                    f'alt="{html.escape(alt)}">')
        # Left online by the fetcher's size cap, or simply absent. Say which, and
        # give a way to it -- a broken image icon explains nothing.
        url = f"{raw_base}/{urllib.parse.quote(rel)}"
        label = html.escape(alt or pathlib.PurePosixPath(rel).name)
        return (f'<a class="offimg" href="{url}" target="_blank" rel="noopener">'
                f'🖼 {label} — view online</a>')

    md.add_render_rule("link_open", link_open)
    md.add_render_rule("image", image)
    return md


_IMG_TAG = re.compile(r"<img\b[^>]*>", re.I)
_SRC_ATTR = re.compile(r"""\bsrc\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)
_ALT_ATTR = re.compile(r"""\balt\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)


def _fix_raw_imgs(product: str, out: str) -> str:
    """Rewrite <img> tags the markdown renderer never saw.

    THESE ARE WRITTEN AS RAW HTML in the wikis -- 37 of them across the three,
    against 75 written as `![](...)`. markdown-it passes raw HTML through as an
    opaque block, so the image render rule never fires on them and their `src`
    stays relative.

    A regex over the FINISHED HTML is safe here in a way it would not be over the
    markdown source: by this point a fenced example containing an <img> has been
    escaped to `&lt;img`, so a literal `<img` in the output can only be a real
    tag. That is the same reason links are NOT done this way -- a bare URL inside
    a code block stays a bare URL, and rewriting it would corrupt an example.
    """
    owner, repo = WIKIS.get(product, ("greghulette", product))
    raw_base = f"https://raw.githubusercontent.com/wiki/{owner}/{repo}"
    base = root(product)

    def one(m: re.Match) -> str:
        tag = m.group(0)
        sm = _SRC_ATTR.search(tag)
        if not sm:
            return tag
        src = (sm.group(1) or sm.group(2) or "").strip()
        if not src or src.startswith(("http://", "https://", "data:", "/")):
            return tag
        rel = src.lstrip("./").split("#", 1)[0]
        if (base / rel).is_file():
            return tag[:sm.start(0)] + f'src="/{"wiki"}/{product}/{urllib.parse.quote(rel)}"'                    + tag[sm.end(0):]
        am = _ALT_ATTR.search(tag)
        alt = (am.group(1) or am.group(2) or "") if am else ""
        label = html.escape(alt or pathlib.PurePosixPath(rel).name)
        url = f"{raw_base}/{urllib.parse.quote(rel)}"
        return (f'<a class="offimg" href="{url}" target="_blank" rel="noopener">'
                f'🖼 {label} — view online</a>')

    return _IMG_TAG.sub(one, out)


def render_page(product: str, name: str) -> tuple[str, str] | None:
    """(title, html body) for one page, or None when there is no such page."""
    f = _page_file(product, name)
    if f is None:
        return None
    text = f.read_text(encoding="utf-8", errors="replace")
    body = _fix_raw_imgs(product, _make_md(product).render(_wikilinks_to_md(text)))
    # The first heading is a better title than the filename when they differ.
    m = re.search(r"^#\s+(.+)$", text, re.M)
    title = (m.group(1) if m else name.replace("-", " ")).strip()
    return title, body


def render_sidebar(product: str) -> str:
    """The wiki's own _Sidebar, or a plain page list when it has none."""
    f = _page_file(product, "_Sidebar")
    if f is not None:
        text = f.read_text(encoding="utf-8", errors="replace")
        return _make_md(product).render(_wikilinks_to_md(text))
    items = "".join(
        f'<li><a href="/wiki/{product}/{urllib.parse.quote(p)}">'
        f'{html.escape(p.replace("-", " "))}</a></li>'
        for p in next((w["pages"] for w in available() if w["product"] == product), []))
    return f"<ul>{items}</ul>"
