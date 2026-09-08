#!/usr/bin/env python3
"""Does the offline docs viewer actually work offline?

    python tools/smoke_wiki.py

Touches no network. Renders every page of every downloaded wiki through the real
src/wikidocs.py and checks that nothing in the output still points at github.com
or at a path only a browser sitting on github.com could resolve.

THE FAILURE THIS GUARDS is quiet and total: a page renders, looks perfect, and
every link on it leaves for the internet -- which is not there, because the whole
reason the docs were downloaded is that there is no internet. Nothing about the
page says so until you click.

Skipped when nothing has been downloaded, because that is a legitimate state and
a smoke test that fails on a clean checkout teaches people to ignore it.
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import wikidocs                                                     # noqa: E402

# An <img> whose src is neither rooted at our own /wiki/ nor absolute-external.
RE_REL_IMG = re.compile(r'<img[^>]+src="(?![/h])[^"]*"', re.I)
# A link still aimed at the wiki on github.com rather than at this app.
RE_GH_WIKI = re.compile(r'href="https://github\.com/[^"]*/wiki/', re.I)
# Wiki-link syntax that never got converted.
RE_RAW_WIKILINK = re.compile(r"\[\[[^\]]+\]\]")


def main() -> int:
    wikis = [w for w in wikidocs.available() if w["have"]]
    if not wikis:
        print("no wikis downloaded — run tools/fetch_wiki.py --wiki all")
        print("wiki: SKIPPED")
        return 0

    bad = 0
    total = 0
    for w in wikis:
        product = w["product"]
        problems: list[str] = []
        for page in w["pages"]:
            r = wikidocs.render_page(product, page)
            if r is None:
                problems.append(f"{page}: would not render")
                continue
            total += 1
            _title, body = r
            for label, rx in (("relative <img>", RE_REL_IMG),
                              ("link still on github.com", RE_GH_WIKI),
                              ("un-converted [[wikilink]]", RE_RAW_WIKILINK)):
                hits = rx.findall(body)
                if hits:
                    problems.append(f"{page}: {len(hits)} {label} — e.g. {hits[0][:70]}")

        sidebar = wikidocs.render_sidebar(product)
        if product != "wcb" and 'href="/wiki/' not in sidebar:
            # The WCB sidebar is plain markdown links and still resolves; the
            # other two are [[wikilinks]] and would silently render as text if
            # the conversion regressed.
            problems.append("sidebar has no links back into the app")

        bad += len(problems)
        mark = "PASS" if not problems else "FAIL"
        print(f"  {mark}  {w['title']}: {len(w['pages'])} pages")
        for p in problems[:6]:
            print(f"          {p}")
        if len(problems) > 6:
            print(f"          … and {len(problems) - 6} more")

    # A page name is attacker-controlled in the sense that it arrives in a URL,
    # and the wiki directory sits next to everything else the app owns.
    escapes = ["../../src/host.py", "..", "../paths", "a/b", "..\\host.py"]
    # NOT a for/else: that runs whenever the loop is not broken out of, so it
    # printed PASS even on the runs where an escape had just been accepted.
    leaked = [n for n in escapes if wikidocs.render_page("wcb", n) is not None]
    for name in leaked:
        print(f"  FAIL  path escape accepted: {name!r}")
    bad += len(leaked)
    if not leaked:
        print(f"  PASS  {len(escapes)} path-escape attempts all refused")

    print()
    print(f"rendered {total} page(s)")
    print("wiki: " + ("OK" if not bad else f"{bad} PROBLEM(S)"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
