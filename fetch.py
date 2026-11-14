"""Fetch real-estate notices of the Agenzia del Demanio (sales, auctions, leases, concessions of
state property) from https://www.agenziademanio.it/it/gare-aste/immobiliare/, keeping slim,
person-free records.

Usage: python3 fetch.py [--pages N] [--all] [--detail-since YYYY-MM-DD] [--max-detail N]
Reads listing pages (?p=1, 2, ...; newest first) until a page brings nothing new (at least --pages
pages), then fetches the detail page of every new notice, or of a notice whose listed deadline changed.
--all walks every listing page (~20 pages; the service does it daily, so a deadline moved on an old notice is seen). Writes data/notices.json (dict slug -> record).
Never stored: the name and e-mail of the person responsible (RUP). PDFs are linked, never downloaded.
Refuses to run while data/BLOCKED exists.
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

BASE = "https://www.agenziademanio.it"
LIST = BASE + "/it/gare-aste/immobiliare/"
UA = "OssianBot/0.1 (+https://ossian.cloud; ossian@ossian.cloud)"
PAUSE = 3  # seconds between requests
ROME = ZoneInfo("Europe/Rome")
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "notices.json")
BLOCKED = os.path.join(HERE, "data", "BLOCKED")
BLOCKLIST = os.path.join(HERE, "blocklist.txt")  # slugs removed on request, one per line

MONTHS = {m: i + 1 for i, m in enumerate("gennaio febbraio marzo aprile maggio giugno luglio agosto "
                                          "settembre ottobre novembre dicembre".split())}
JMONTHS = {m: i + 1 for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split())}
# detail fields kept; "nominativo" (RUP name) and "email" are personal data and never read
FIELDS = {"descrizione": "description", "tipologia": "type", "regione": "region", "provincia": "province",
          "comune": "comune", "stazione": "office", "datapubblicazione": "published",
          "datascadenza": "deadline", "importo": "amount"}
# a description that names a private person (current occupant, previous tenant) is cut before the name
PERSON = re.compile(r"\b(?:sig(?:\.|nor)\w*|sig\.ra|dott(?:\.|or)\w*|avv\.|geom\.|ing\.|"
                    r"ditta individuale|eredi|coniugi|nato a|nata a|codice fiscale)\s", re.I)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE = re.compile(r"(?<![\d,.])(?:\+?39[\s.-]?)?(?:0\d{1,3}[\s.-]?\d{5,8}|3\d{2}[\s.-]?\d{6,7})(?![\d,])")


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "it"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                with open(BLOCKED, "w") as f:
                    f.write(f"{dt.datetime.now().isoformat(timespec='seconds')} HTTP {e.code} on {url}\n")
                sys.exit(f"blocked: HTTP {e.code}; wrote {BLOCKED}")
            if e.code == 404:
                return None
            err = e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            err = e
        time.sleep(PAUSE * (attempt + 2))
    raise RuntimeError(f"{url}: {err}")


def text(s):
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def clean(s):
    """Text safe to republish: no e-mail addresses, phone numbers or private persons' names."""
    s = EMAIL.sub("[…]", s)
    s = PHONE.sub("[…]", s)
    m = PERSON.search(s)
    if m:
        s = s[:m.start()].rstrip(" ,;:(") + " […]"
    return s


def it_date(s):
    """'06 novembre 2026' or '05 dicembre 2026 12:00' -> ISO with Rome offset (date only if no time)."""
    m = re.match(r"(\d{1,2}) (\w+) (\d{4})(?: (\d{1,2}):(\d{2}))?", s.strip())
    if not m or m.group(2).lower() not in MONTHS:
        return None
    d = dt.datetime(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1)),
                    int(m.group(4) or 0), int(m.group(5) or 0), tzinfo=ROME)
    return d.isoformat() if m.group(4) else d.date().isoformat()


def java_date(s):
    """'Fri Nov 06 09:53:32 CET 2026' -> ISO with Rome offset (the zone name is Rome's own)."""
    m = re.match(r"\w{3} (\w{3}) (\d{2}) (\d{2}):(\d{2}):(\d{2}) \w+ (\d{4})", s.strip())
    if not m or m.group(1) not in JMONTHS:
        return None
    return dt.datetime(int(m.group(6)), JMONTHS[m.group(1)], int(m.group(2)), int(m.group(3)),
                       int(m.group(4)), int(m.group(5)), tzinfo=ROME).isoformat()


def parse_list(page):
    """Cards on a listing page: [{slug, title, type, published, deadline, description}]."""
    out = []
    for card in re.findall(r'<div class="card-body">(.*?)</div>', page, re.S):
        a = re.search(r'href="/it/gare-aste/immobiliare/gara/([^"]+)"', card)
        if not a:
            continue
        r = {"slug": urllib.parse.unquote(a.group(1)),
             "title": text(re.search(r'<h2 class="card-title[^"]*">(.*?)</h2>', card, re.S).group(1))}
        for label, val in re.findall(r'<p class="color_text_gare[^"]*">\s*<(?:strong|span)[^>]*>(.*?)</(?:strong|span)>(.*?)</p>',
                                     card, re.S):
            label, val = text(label).rstrip(": "), text(val)
            if label == "Tipo procedura":
                r["type"] = val
            elif label == "Oggetto della gara":
                r["subject"] = val
            elif label == "Data Pubblicazione bando":
                r["published"] = it_date(val)
            elif label == "Termine per partecipare":
                r["deadline"] = it_date(val)
            elif label == "Descrizione":
                r["description"] = clean(val)
        out.append(r)
    return out


def parse_detail(page):
    r = {}
    m = re.search(r'<div\s+id="detail"\s*>(.*?)<div class="documentigara">', page, re.S) \
        or re.search(r'<div\s+id="detail"\s*>(.*?)</main>', page, re.S)
    if not m:
        return None
    body = m.group(1)
    # one chunk per field paragraph, up to its last </p>: a paragraph can hold nested <p> (an extension note)
    for chunk in re.split(r'<p class="', body)[1:]:
        m = re.match(r'(\w+)">\s*<span[^>]*>[^<]*</span>(.*)</p>', chunk, re.S)
        if not m or m.group(1) not in FIELDS:
            continue
        key, val = FIELDS[m.group(1)], text(m.group(2))
        if key == "description" and r.get(key) and val:
            val = r[key].rstrip(" .") + ". " + val  # several description paragraphs: keep them all
        if val or key not in r:
            r[key] = val
    for k in ("published", "deadline"):
        if k in r:
            r[k] = java_date(r[k]) or it_date(r[k]) or r[k]  # Italian when asked in Italian
    if "description" in r:
        r["description"] = clean(r["description"])
    if "amount" in r:
        r["amount"] = clean(r["amount"])
    docs = re.search(r'<div class="documentigara">(.*?)</div>', page, re.S)
    r["docs"] = []
    if docs:
        for label, href, name in re.findall(r'<p class="documento">\s*<span>(.*?)</span>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                                            docs.group(1), re.S):
            r["docs"].append({"label": text(label).rstrip(": "), "name": text(name),
                              "url": urllib.parse.urljoin(BASE, html.unescape(href))})
    r["sale_site"] = bool(re.search(r'href="https?://venditaimmobili\.agenziademanio\.it', body))
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=2, help="read at least this many listing pages")
    ap.add_argument("--all", action="store_true", help="walk every listing page (backfill)")
    ap.add_argument("--detail-since", default=None,
                    help="fetch detail pages only for notices with a deadline on or after this date")
    ap.add_argument("--max-detail", type=int, default=60)
    ap.add_argument("--refetch-cut", action="store_true", help="re-read notices whose description was cut")
    ap.add_argument("--refetch-open", action="store_true", help="re-read every listed notice still open")
    a = ap.parse_args()
    if os.path.exists(BLOCKED):
        sys.exit(f"BLOCKED file present: {open(BLOCKED).read().strip()}")
    os.makedirs(os.path.dirname(DATA), exist_ok=True)
    store = json.load(open(DATA)) if os.path.exists(DATA) else {}
    blocked = set()
    if os.path.exists(BLOCKLIST):
        blocked = {l.strip() for l in open(BLOCKLIST) if l.strip() and not l.startswith("#")}
    now = dt.datetime.now(ROME).isoformat(timespec="seconds")
    since = a.detail_since or (dt.date.today() - dt.timedelta(days=1)).isoformat()

    todo, seen, p = [], 0, 1
    while True:
        page = get(f"{LIST}?p={p}")
        cards = parse_list(page or "")
        if not cards:
            break
        new = 0
        for c in cards:
            seen += 1
            if c["slug"] in blocked:
                continue
            old = store.get(c["slug"])
            if old is None:
                new += 1
                store[c["slug"]] = {**c, "first_seen": now, "detail": False}
            else:
                old["last_seen"] = now
                if c.get("deadline") and c["deadline"] != old.get("list_deadline") and old.get("list_deadline"):
                    old["detail"] = False  # deadline (day or hour) moved: re-read the page
                if c.get("description") and old.get("list_description") \
                        and c["description"] != old["list_description"]:
                    old["detail"] = False  # card text changed: re-read the page
                for k in ("title", "type"):
                    if c.get(k):
                        old[k] = c[k]
                if c.get("description") and not old.get("detail"):
                    old["description"] = c["description"]  # the detail page's (fuller) text wins once read
            rec = store[c["slug"]]
            rec["list_deadline"] = c.get("deadline")
            rec["list_description"] = c.get("description")
            rec["last_seen"] = now
            if a.refetch_open and (c.get("deadline") or "") >= now:
                rec["detail"] = False
            if a.refetch_cut and (rec.get("description") or "").endswith(" […]"):
                rec["detail"] = False
            if not rec.get("detail") and (c.get("deadline") or "9999") >= since:
                todo.append(c["slug"])
        if not a.all and p >= a.pages and new == 0:
            break
        p += 1
        time.sleep(PAUSE)
    print(f"{p} listing pages, {seen} cards, {len(store)} stored, {len(todo)} detail pages to fetch")
    if a.all:  # a full walk sees every listed notice: an open one it missed was withdrawn or moved
        gone = [s for s, r in store.items() if r.get("last_seen") != now and (r.get("deadline") or "") >= now]
        print(f"open but no longer listed: {len(gone)}" + (f" ({', '.join(g[:50] for g in gone[:5])})" if gone else ""))

    done = 0
    for slug in todo[:a.max_detail]:
        time.sleep(PAUSE)
        page = get(LIST + "gara/" + urllib.parse.quote(slug, safe="-._~"))
        d = parse_detail(page) if page else None
        if d is None:
            print(f"no detail: {slug}")
            continue
        changed = any(store[slug].get(k) != v for k, v in d.items())
        store[slug].update(d)
        store[slug]["detail"] = True
        if changed or not store[slug].get("detail_at"):
            store[slug]["detail_at"] = now  # feed <updated>: only when the page really changed (daily re-reads)
        done += 1
        if done % 25 == 0:
            print(f"{done}/{min(len(todo), a.max_detail)}", flush=True)
            json.dump(store, open(DATA + ".tmp", "w"), ensure_ascii=False, indent=0)
            os.replace(DATA + ".tmp", DATA)
    json.dump(store, open(DATA + ".tmp", "w"), ensure_ascii=False, indent=0)
    os.replace(DATA + ".tmp", DATA)
    print(f"fetched {done} detail pages, total {len(store)}")


if __name__ == "__main__":
    main()
