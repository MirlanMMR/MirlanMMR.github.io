#!/usr/bin/env python3
"""Static multi-language builder for the MMR Lab site.

English pages in the repo root are the source of truth. Translations live in
i18n/<lang>/<page>.json as {id: translated text}. `build` writes
/<lang>/... pages, updates generated regions (hreflang, language switcher) in
the English pages and regenerates both sitemaps.

Commands:
  extract                      rewrite i18n/en.json (id -> English text)
  todo <lang> [page ...]       print untranslated units as JSON (ids -> English)
  status                       coverage table (language x page)
  check [lang ...]             validate translations (tags/links preserved)
  bootstrap <lang> <page>      import units from an existing hand-written page
  build [lang ...]             generate pages (only pages with 100% coverage)
  review <lang> [site|legal]   write review/<lang>-<part>.txt (EN + translation) for an outside check
  set <lang> <id> <text>       replace one translation by unit id
"""
import sys, os, re, json, hashlib, html, datetime
from html.parser import HTMLParser
from urllib.parse import urljoin

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = "https://mirlanmmr.github.io"
I18N = os.path.join(ROOT, "i18n")
TODAY = datetime.date.today().isoformat()

# page key -> English source file
PAGES = {
    "home": "index.html",
    "apps": "apps/index.html",
    "slowread": "apps/slowread/index.html",
    "ticker247": "apps/ticker247/index.html",
    "happypouch": "apps/happypouch/index.html",
    "calculator": "apps/calculator/index.html",
    "easycounting": "apps/easycounting/index.html",
    "ec-privacy": "easycounting/privacy-policy.html",
    "ec-terms": "easycounting/terms-of-use.html",
    "calc-privacy": "calculator/privacy-policy.html",
    "calc-terms": "calculator/terms.html",
    "tk-privacy": "ticker247/privacy-policy.html",
    "tk-terms": "ticker247/terms-of-use.html",
    "sr-terms": "slowread/terms-of-use.html",
}
LEGAL = {"ec-privacy", "ec-terms", "calc-privacy", "calc-terms", "tk-privacy", "tk-terms", "sr-terms"}
EXTRA_SITEMAP = ["/apps/happypouch/languages/"]


def page_url(key):
    f = PAGES[key]
    if f.endswith("index.html"):
        return "/" + f[: -len("index.html")]
    return "/" + f


URL_TO_KEY = {page_url(k): k for k in PAGES}

# ---------------------------------------------------------------- languages
LANGS = json.load(open(os.path.join(ROOT, "tools", "langs.json"), encoding="utf-8"))
LANG = {l["code"]: l for l in LANGS}
ORDER = [l["code"] for l in LANGS]  # includes "en"

KEEP = set(
    json.load(open(os.path.join(I18N, "keep.json"), encoding="utf-8"))
    if os.path.exists(os.path.join(I18N, "keep.json"))
    else []
)

# ---------------------------------------------------------------- html tree
VOID = {"meta", "link", "img", "br", "hr", "input", "source", "area", "base", "col", "embed", "track", "wbr"}
SKIP = {"script", "style", "svg", "noscript"}
ATTRS = ("alt", "aria-label", "title", "placeholder", "data-copied")


class Node:
    __slots__ = ("tag", "attrs", "kids", "ld")

    def __init__(self, tag, attrs):
        self.tag, self.attrs, self.kids = tag, attrs, []

    def get(self, k):
        for a, v in self.attrs:
            if a == k:
                return v
        return None

    def set(self, k, v):
        self.attrs = [(a, (v if a == k else b)) for a, b in self.attrs]


class Raw:
    def __init__(self, t):
        self.t = t


class Parser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.root = Node("#root", [])
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        n = Node(tag, attrs)
        self.stack[-1].kids.append(n)
        if tag not in VOID:
            self.stack.append(n)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].kids.append(Node(tag, attrs))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, d):
        self.stack[-1].kids.append(d)

    def handle_entityref(self, n):
        self.stack[-1].kids.append("&%s;" % n)

    def handle_charref(self, n):
        self.stack[-1].kids.append("&#%s;" % n)

    def handle_comment(self, d):
        self.stack[-1].kids.append(Raw("<!--%s-->" % d))

    def handle_decl(self, d):
        self.stack[-1].kids.append(Raw("<!%s>" % d))


def esc_attr(v):
    return v.replace("&", "&amp;").replace('"', "&quot;")


def ser(n):
    if isinstance(n, str):
        return n
    if isinstance(n, Raw):
        return n.t
    a = "".join(" %s" % k if v is None else ' %s="%s"' % (k, esc_attr(v)) for k, v in n.attrs)
    if n.tag == "#root":
        return "".join(ser(k) for k in n.kids)
    if n.tag in VOID:
        return "<%s%s>" % (n.tag, a)
    return "<%s%s>%s</%s>" % (n.tag, a, "".join(ser(k) for k in n.kids), n.tag)


def inner(n):
    return "".join(ser(k) for k in n.kids)


def norm(s):
    return re.sub(r"\s+", " ", s).strip()


def has_letters(s):
    return re.search(r"[^\W\d_]", s) is not None


def uid(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:6]


def parse(text):
    p = Parser()
    p.feed(text)
    p.close()
    return p.root


REGION = re.compile(r"<!--i18n:(\w+)-->.*?<!--/i18n:\1-->", re.S)


def mask_regions(text):
    return REGION.sub(lambda m: "<!--@@%s@@-->" % m.group(1), text)


# ---------------------------------------------------------------- units
class Unit:
    def __init__(self, kind, obj, key, text):
        self.kind, self.obj, self.key, self.text = kind, obj, key, text
        self.id = uid(text)

    @property
    def keep(self):
        return (not has_letters(self.text)) or self.text in KEEP


def ld_items(o, out, typ=None):
    if isinstance(o, dict):
        t = o.get("@type", typ)
        for k, v in o.items():
            if isinstance(v, str):
                if k in ("description", "text") or (k == "name" and t == "Question"):
                    out.append((o, k, v))
            else:
                ld_items(v, out, t)
    elif isinstance(o, list):
        for v in o:
            ld_items(v, out, typ)


def collect(n, out):
    if isinstance(n, (str, Raw)):
        return
    for k in ATTRS:
        v = n.get(k)
        if v and has_letters(v) and n.tag != "#root":
            out.append(Unit("attr", n, k, norm(v)))
    if n.tag == "meta":
        nm = (n.get("name") or n.get("property") or "").lower()
        if nm in ("description", "og:title", "og:description", "twitter:title", "twitter:description"):
            v = n.get("content")
            if v and has_letters(v):
                out.append(Unit("attr", n, "content", norm(v)))
        return
    if n.tag == "script" and n.get("type") == "application/ld+json":
        data = json.loads(inner(n))
        items = []
        ld_items(data, items)
        n.ld = data  # type: ignore[attr-defined]
        for o, k, v in items:
            out.append(Unit("ld", (n, o), k, norm(v)))
        return
    if n.tag in SKIP:
        return
    if any(isinstance(k, str) and k.strip() for k in n.kids):
        t = norm(inner(n))
        if has_letters(re.sub(r"<[^>]+>|&\w+;|&#\d+;", "", t)):
            out.append(Unit("node", n, None, t))
        return
    for k in n.kids:
        collect(k, out)


def legal_view(text, section="en"):
    """legal pages hold several language sections; keep one of them"""
    m = re.search(r'<section id="%s"' % section, text)
    end = text.index("</section>", m.start()) + len("</section>")
    head = text[: text.index('<section id=')]
    tail = text[text.rindex("</section>") + len("</section>"):]
    return head + text[m.start():end] + "\n" + tail


def load_source(key):
    path = os.path.join(ROOT, PAGES[key])
    text = open(path, encoding="utf-8").read()
    if key in LEGAL:
        text = legal_view(text)
    return text


def en_units(key):
    root = parse(mask_regions(load_source(key)))
    out = []
    collect(root, out)
    return root, out


def need_ids(key):
    """ordered unique ids that need a translation"""
    seen, res = set(), []
    for u in en_units(key)[1]:
        if not u.keep and u.id not in seen:
            seen.add(u.id)
            res.append((u.id, u.text))
    return res


# ---------------------------------------------------------------- translations
def load_tr(lang):
    d = {}
    base = os.path.join(I18N, lang)
    if os.path.isdir(base):
        for f in sorted(os.listdir(base)):
            if f.endswith(".json"):
                d.update(json.load(open(os.path.join(base, f), encoding="utf-8")))
    return d


def missing(lang, key):
    tr = load_tr(lang)
    return [(i, t) for i, t in need_ids(key) if i not in tr]


def skeleton(s):
    return [re.sub(r"\s+", " ", t) for t in re.findall(r"<[^>]+>", s)]


# ---------------------------------------------------------------- url handling
def localize_path(path, lang, built):
    if lang == "en":
        return path
    k = URL_TO_KEY.get(path)
    if k and (k, lang) in built:
        return "/%s%s" % (lang, path)
    return path


def fix_urls(text, lang, page_path, built):
    def rep(m):
        attr, val = m.group(1), m.group(2)
        v = html.unescape(val)
        if v.startswith(("mailto:", "tel:", "#", "data:", "javascript:")):
            return m.group(0)
        absolute = False
        if v.startswith(SITE):
            v, absolute = v[len(SITE):] or "/", True
        elif re.match(r"^[a-z][a-z0-9+.-]*:", v, re.I) or v.startswith("//"):
            return m.group(0)
        frag = ""
        if "#" in v:
            v, frag = v.split("#", 1)
            frag = "#" + frag
        qs = ""
        if "?" in v:
            v, qs = v.split("?", 1)
            qs = "?" + qs
        path = urljoin(page_path, v) if v else page_path
        if path.endswith("/index.html"):
            path = path[: -len("index.html")]
        if attr == "href" or absolute:
            path = localize_path(path, lang, built)
        out = (SITE if absolute else "") + path + qs + frag
        return '%s="%s"' % (attr, esc_attr(out))

    return re.sub(r'\b(href|src)="([^"]*)"', rep, text)


# ---------------------------------------------------------------- build parts
def built_set(langs=None):
    """set of (key, lang) with full coverage; English always present"""
    res = set()
    for k in PAGES:
        res.add((k, "en"))
    for l in ORDER:
        if l == "en":
            continue
        if langs is not None and l not in langs:
            continue
        for k in PAGES:
            if not missing(l, k):
                res.add((k, l))
    return res


def switcher(key, lang, built):
    items = []
    for l in ORDER:
        if (key, l) not in built:
            continue
        href = page_url(key) if l == "en" else "/%s%s" % (l, page_url(key))
        cur = ' aria-current="true"' if l == lang else ""
        items.append('<li><a href="%s" hreflang="%s" lang="%s" data-lang="%s"%s>%s</a></li>'
                     % (href, LANG[l]["hreflang"], LANG[l]["hreflang"], l, cur, LANG[l]["name"]))
    return ('<details class="langs"><summary lang="%s">%s</summary><ul>%s</ul></details>'
            % (LANG[lang]["hreflang"], LANG[lang]["name"], "".join(items)))


def alternates(key, lang, built):
    out = ['<link rel="stylesheet" href="/assets/i18n.css">']
    for l in ORDER:
        if (key, l) in built:
            href = SITE + (page_url(key) if l == "en" else "/%s%s" % (l, page_url(key)))
            out.append('<link rel="alternate" hreflang="%s" href="%s">' % (LANG[l]["hreflang"], href))
    out.append('<link rel="alternate" hreflang="x-default" href="%s%s">' % (SITE, page_url(key)))
    return "\n".join(out)


def script_tag(key, lang, built):
    m = {}
    for l in ORDER:
        if (key, l) in built:
            href = page_url(key) if l == "en" else "/%s%s" % (l, page_url(key))
            m[l] = [href, LANG[l]["name"], LANG[l]["banner"]]
    return ('<script src="/assets/i18n.js" defer data-cur="%s" data-map=\'%s\'></script>'
            % (lang, json.dumps(m, ensure_ascii=False, separators=(",", ":")).replace("'", "&#39;")))


def fill_regions(text, key, lang, built):
    gens = {
        "alternates": alternates(key, lang, built),
        "switcher": switcher(key, lang, built),
        "script": script_tag(key, lang, built),
    }

    def rep(m):
        name = m.group(1)
        return "<!--i18n:%s-->%s<!--/i18n:%s-->" % (name, gens[name], name)

    return REGION.sub(rep, text), None


def translate_page(key, lang, built):
    root, units = en_units(key)
    tr = load_tr(lang)
    for u in units:
        if u.keep:
            continue
        t = tr[u.id]
        if u.kind == "node":
            t = re.sub(r"&(?!#?\w+;)", "&amp;", t)
            u.obj.kids = [t]
        elif u.kind == "attr":
            u.obj.set(u.key, html.unescape(t))
        else:
            n, o = u.obj
            o[u.key] = html.unescape(t)
    # ld+json rewrite
    def walk(n):
        if isinstance(n, (str, Raw)):
            return
        if n.tag == "script" and n.get("type") == "application/ld+json" and hasattr(n, "ld"):
            d = n.ld
            def fix(o):
                if isinstance(o, dict):
                    for k, v in list(o.items()):
                        if isinstance(v, str) and v.startswith(SITE):
                            p = v[len(SITE):] or "/"
                            o[k] = SITE + localize_path(p, lang, built)
                        else:
                            fix(v)
                    if o.get("@type") == "SoftwareApplication" or o.get("@type") == "CollectionPage":
                        o["inLanguage"] = LANG[lang]["hreflang"]
                elif isinstance(o, list):
                    for v in o:
                        fix(v)
            fix(d)
            n.kids = ["\n" + json.dumps(d, ensure_ascii=False, indent=2) + "\n"]
            return
        for k in n.kids:
            walk(k)
    walk(root)
    # html lang
    for k in root.kids:
        if isinstance(k, Node) and k.tag == "html":
            k.set("lang", LANG[lang]["hreflang"])
    out = ser(root)
    if key in LEGAL:
        out = out.replace('<section id="en" lang="en">', '<section id="%s" lang="%s">' % (lang, LANG[lang]["hreflang"]))
        note = LANG[lang].get("legal_note")
        if note:
            out = out.replace("</h1>", '</h1>\n<div class="updated">%s</div>' % note, 1)
    out = fix_urls(out, lang, page_url(key), built)
    out = re.sub(r"<!--@@(\w+)@@-->", lambda m: "<!--i18n:%s--><!--/i18n:%s-->" % (m.group(1), m.group(1)), out)
    out, _ = fill_regions(out, key, lang, built)
    return out


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    old = open(path, encoding="utf-8").read() if os.path.exists(path) else None
    if old != text:
        open(path, "w", encoding="utf-8").write(text)
        return True
    return False


def cmd_build(args):
    built = built_set(set(args) if args else None)
    changed = 0
    for key in PAGES:
        # English page: only refresh generated regions
        p = os.path.join(ROOT, PAGES[key])
        t = open(p, encoding="utf-8").read()
        t2, _ = fill_regions(t, key, "en", built)
        changed += write(p, t2)
        for l in ORDER:
            if l == "en" or (key, l) not in built:
                continue
            out = translate_page(key, l, built)
            changed += write(os.path.join(ROOT, l, PAGES[key]), out)
    # sitemaps
    urls = []
    for key in PAGES:
        urls.append((key, "en"))
        for l in ORDER:
            if l != "en" and (key, l) in built:
                urls.append((key, l))

    def loc(key, l):
        return SITE + (page_url(key) if l == "en" else "/%s%s" % (l, page_url(key)))

    rich = ['<?xml version="1.0" encoding="UTF-8"?>',
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:xhtml="http://www.w3.org/1999/xhtml">']
    plain = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for key, l in urls:
        alts = "".join('<xhtml:link rel="alternate" hreflang="%s" href="%s"/>' % (LANG[x]["hreflang"], loc(key, x))
                       for x in ORDER if (key, x) in built)
        alts += '<xhtml:link rel="alternate" hreflang="x-default" href="%s"/>' % loc(key, "en")
        rich.append("  <url><loc>%s</loc><lastmod>%s</lastmod>%s</url>" % (loc(key, l), TODAY, alts))
        plain.append("  <url><loc>%s</loc></url>" % loc(key, l))
    for e in EXTRA_SITEMAP:
        rich.append("  <url><loc>%s%s</loc><lastmod>2026-11-25</lastmod></url>" % (SITE, e))
        plain.append("  <url><loc>%s%s</loc></url>" % (SITE, e))
    rich.append("</urlset>")
    plain.append("</urlset>")
    changed += write(os.path.join(ROOT, "sitemap.xml"), "\n".join(rich) + "\n")
    changed += write(os.path.join(ROOT, "sitemap-pages.xml"), "\n".join(plain) + "\n")
    n = len([1 for k, l in built if l != "en"])
    print("built %d localized pages, %d files changed" % (n, changed))


def cmd_extract(_):
    cat = {}
    for key in PAGES:
        for u in en_units(key)[1]:
            if u.keep:
                continue
            if u.id in cat and cat[u.id] != u.text:
                print("ID COLLISION", u.id, u.text, cat[u.id])
            cat[u.id] = u.text
    os.makedirs(I18N, exist_ok=True)
    json.dump(cat, open(os.path.join(I18N, "en.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    print("%d unique units" % len(cat))


def cmd_todo(args):
    lang, pages = args[0], args[1:] or list(PAGES)
    out, seen = {}, set()
    for k in pages:
        for i, t in missing(lang, k):
            if i not in seen:
                seen.add(i)
                out[i] = t
    print(json.dumps(out, ensure_ascii=False, indent=0))
    print("# %d units" % len(out), file=sys.stderr)


def cmd_status(_):
    print("%-6s" % "lang" + " ".join("%-12s" % k for k in PAGES))
    for l in ORDER:
        if l == "en":
            continue
        row = []
        for k in PAGES:
            need = need_ids(k)
            miss = len(missing(l, k))
            row.append("%-12s" % ("ok" if not miss else "%d/%d" % (len(need) - miss, len(need))))
        print("%-6s" % l + " ".join(row))


def cmd_check(args):
    bad = 0
    cat = {}
    for key in PAGES:
        for u in en_units(key)[1]:
            if not u.keep:
                cat[u.id] = (u.kind, u.text)
    for l in args or [x for x in ORDER if x != "en"]:
        tr = load_tr(l)
        for i, t in tr.items():
            if i not in cat:
                continue
            kind, en = cat[i]
            if kind == "node":
                if skeleton(en) != skeleton(t):
                    print("[%s] %s tags differ:\n  en: %s\n  tr: %s" % (l, i, en, t))
                    bad += 1
                elif re.findall(r'href="[^"]*"', en) != re.findall(r'href="[^"]*"', t):
                    print("[%s] %s hrefs differ" % (l, i))
                    bad += 1
            elif "<" in t and kind != "node":
                print("[%s] %s html in plain unit: %s" % (l, i, t))
                bad += 1
    print("check done, %d problems" % bad)


def cmd_bootstrap(args):
    lang, key = args[0], args[1]
    root_en, ue = en_units(key)
    if lang == "ru" and key == "home":
        src = home_variant(load_source_raw_home(), "ru")
    elif key in LEGAL:
        src = legal_view(open(os.path.join(ROOT, PAGES[key]), encoding="utf-8").read(), lang)
    else:
        p = os.path.join(ROOT, lang, PAGES[key])
        src = open(p, encoding="utf-8").read()
    if lang == "ru" and key == "home":
        src = src.replace('id="copy-card"', 'id="copy-card" data-copied="Скопировано ✓"')
    src = re.sub(r'<(a|span) class="langsw".*?</\1>', "", src, flags=re.S)
    src = re.sub(r'<link rel="alternate"[^>]*>', "", src)
    src = re.sub(r'<div class="lang" role="group".*?</div>', "", src, flags=re.S)
    root_x = parse(mask_regions(src))
    ux = []
    collect(root_x, ux)
    ue = [u for u in ue if not u.keep]
    ux = [u for u in ux if not u.keep]
    if len(ux) != len(ue):
        print("count mismatch %s/%s: en=%d x=%d" % (lang, key, len(ue), len(ux)))
        for i in range(min(len(ue), len(ux))):
            a, b = ue[i].text[:50], ux[i].text[:50]
            print(i, "|", a, "|", b)
            if i > 80:
                break
        return
    res, skipped = {}, 0
    for a, b in zip(ue, ux):
        t = b.text
        if a.kind == "node":
            ea = re.findall(r'href="([^"]*)"', a.text)
            eb = re.findall(r'href="([^"]*)"', t)
            if len(ea) == len(eb):
                it = iter(ea)
                t = re.sub(r'href="([^"]*)"', lambda m: 'href="%s"' % next(it), t)
        res[a.id] = t
    d = os.path.join(I18N, lang)
    os.makedirs(d, exist_ok=True)
    json.dump(res, open(os.path.join(d, key + ".json"), "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    print("bootstrapped %s/%s: %d units" % (lang, key, len(res)))


# ---- home page helper (one-time migration of the bilingual page)
def home_variant(text, lang):
    other = "ru" if lang == "en" else "en"
    text = re.sub(r"<span data-%s[^>]*>.*?</span>" % other, "", text, flags=re.S)
    text = re.sub(r"<span data-%s[^>]*>(.*?)</span>" % lang, r"\1", text, flags=re.S)
    text = re.sub(r"<a [^>]*data-%s[^>]*>.*?</a>" % other, "", text, flags=re.S)
    text = re.sub(r' data-%s(?=[ >])' % lang, "", text)
    return text


def load_source_raw_home():
    return open(os.path.join(ROOT, "tools", "home-bilingual.html"), encoding="utf-8").read()


REVIEW_HEAD = """Please review these translations of website text into {name} ({code}). The source language is English.
The site belongs to a small Android app studio (MMR Lab). The text is {what}.
Check: natural wording for a native reader, grammar, correct terminology, and consistent app names. {legal}
Rules: keep HTML tags and attributes (<b>, <a href>, <code>, <strong>) exactly as they are. App names (Slowread, Ticker 24/7, Happy Pouch, MMR Lab, Calculator: Themes & Widget) stay in Latin script. The app "Math for Kids: 2+2" is called "{appname}" in {name}.
Reply ONLY with the lines that need a change, in this form, one per line:
[id] corrected full text // short reason
Do not repeat lines that are fine. Each entry below has an [id], the English text (EN) and the translation (TR).
"""


def cmd_review(args):
    lang = args[0]
    parts = args[1:] or ["site", "legal"]
    tr = load_tr(lang)
    for part in parts:
        keys = [k for k in PAGES if (k in LEGAL) == (part == "legal")]
        seen, rows = set(), []
        for k in keys:
            for i, t in need_ids(k):
                if i not in seen and i in tr:
                    seen.add(i)
                    rows.append((i, t, tr[i]))
        what = ("marketing pages for five Android apps (a calculator, an ebook reader, a news app, an expense tracker and a children's math app for ages 4-7)"
                if part == "site" else "privacy policies and terms of use for two apps (a calculator and a children's math app for ages 4-7)")
        legal = "This is legal text: it must keep the exact meaning of the English; flag any shift in meaning." if part == "legal" else ""
        out = REVIEW_HEAD.format(name=LANG[lang]["name"], code=lang, what=what, legal=legal,
                                 appname=LANG[lang].get("app_name", "Math for Kids: 2+2"))
        body = "\n".join("[%s]\nEN: %s\nTR: %s\n" % (i, html.unescape(e), html.unescape(t)) for i, e, t in rows)
        d = os.path.join(ROOT, "review")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "%s-%s.txt" % (lang, part))
        open(path, "w", encoding="utf-8").write(out + "\n" + body)
        print("%s: %d units, %d KB" % (path, len(rows), os.path.getsize(path) // 1024))


def cmd_set(args):
    lang, uid_, text = args[0], args[1], " ".join(args[2:])
    base = os.path.join(I18N, lang)
    for f in sorted(os.listdir(base)):
        if f.endswith(".json"):
            p = os.path.join(base, f)
            d = json.load(open(p, encoding="utf-8"))
            if uid_ in d:
                d[uid_] = text
                json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
                print("updated %s in %s" % (uid_, f))
                return
    print("id not found")


if __name__ == "__main__":
    cmds = {"review": cmd_review, "set": cmd_set, "extract": cmd_extract, "todo": cmd_todo, "status": cmd_status, "check": cmd_check,
            "bootstrap": cmd_bootstrap, "build": cmd_build}
    if len(sys.argv) < 2 or sys.argv[1] not in cmds:
        print(__doc__)
        sys.exit(1)
    cmds[sys.argv[1]](sys.argv[2:])
