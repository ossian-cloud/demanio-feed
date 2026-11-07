"""Build the demanio/ pages, Atom feed, CSV and sitemap from data/notices.json.

Usage: python3 build.py OUTDIR   (e.g. /tmp/demanio-out)
"""
import csv
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import zoneinfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "bandi"))  # geo.py: ISTAT names of comuni and provinces
sys.path.insert(0, os.path.join(HERE, "vendor"))  # the same file, copied in the public repo
sys.path.insert(0, HERE)
import geo
import layout
from atom import write_feed

DATA = os.path.join(HERE, "data", "notices.json")
CSV_LIVE = os.path.exists(os.path.join(HERE, "CSV_LIVE"))  # CSVs only after Padre's go-ahead (no licence stated by the source)
BASE = layout.BASE
OFFICIAL_LIST = "https://www.agenziademanio.it/it/gare-aste/immobiliare/"
SOURCE = "Fonte: Agenzia del Demanio, Gare e aste immobiliari (agenziademanio.it)"
DISCLAIMER = ("Estratto non ufficiale: fa fede l'avviso pubblicato dall'Agenzia del Demanio; "
              "verificare termini, requisiti e modalità nel documento ufficiale.")
RIGHTS = f"{SOURCE}. {DISCLAIMER} Feed generato da Ossian, un agente AI, senza alcun legame con l'Agenzia del Demanio."
ROME = zoneinfo.ZoneInfo("Europe/Rome")
GRACE_DAYS = 14  # a closed notice's page stays this long, noindex, then goes
MAX_ENTRIES = 200
REGIONS = ["Abruzzo", "Basilicata", "Calabria", "Campania", "Emilia-Romagna", "Friuli-Venezia Giulia",
           "Lazio", "Liguria", "Lombardia", "Marche", "Molise", "Piemonte", "Puglia", "Sardegna",
           "Sicilia", "Toscana", "Trentino-Alto Adige", "Umbria", "Valle d'Aosta", "Veneto"]
# what the notice offers, from its title and type: (key, label, regex); first match wins
KINDS = [("vendita", "Vendita", r"\bvendit|\basta\b|alienazion|aste\b"),
         ("locazione", "Locazione", r"locazion|affitt"),
         ("concessione", "Concessione", r"concession|comodato|valorizzazion"),
         ("altro", "Altro", r"")]
# never rename, remove or reorder: new columns go at the end (CSV_CHANGES.md)
CSV_COLUMNS = ["id", "titolo", "tipo_offerta", "tipologia", "regione", "provincia", "comune", "struttura",
               "data_pubblicazione", "termine_offerte", "importo", "descrizione", "url_avviso", "url_documenti",
               "prima_vista", "url_pagina"]


def nid(r):
    return hashlib.md5(r["slug"].encode()).hexdigest()[:12]


def short(t, n):
    """Cut a title at a word boundary, with an ellipsis, if longer than n."""
    if len(t) <= n:
        return t
    return t[:n - 1].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"


def page_rel(r):
    return f"avviso/{nid(r)}.html"


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower().replace("'", "")).strip("-")


def kind(r):
    t = f"{r.get('title', '')} {r.get('type', '')}".lower()
    return next(k for k in KINDS if re.search(k[2], t))


def official(r):
    return OFFICIAL_LIST + "gara/" + r["slug"]


def region_of(r):
    if r.get("region"):
        n = geo.norm(r["region"])
        for reg in REGIONS:
            if geo.norm(reg) == n:
                return reg
        if "TRENTINO" in n or "BOLZANO" in n or "TRENTO" in n:
            return "Trentino-Alto Adige"
        if "AOSTA" in n:
            return "Valle d'Aosta"
    if r.get("comune") or r.get("province"):
        return geo.place(r.get("comune"), r.get("province"))[0]
    return None


def place(r):
    """'Pontebba (Udine)', 'provincia di Udine', or ''."""
    prov = geo.place(r.get("comune"), r.get("province"))[1] if r.get("province") else None
    prov = prov or (geo.comune_name(r["province"]) if r.get("province") else None)
    if r.get("comune"):
        c = geo.comune_name(r["comune"])
        return f"{c} ({prov})" if prov and prov != c else c
    return f"provincia di {prov}" if prov else ""


def when(iso, time_too=True):
    """'5 dicembre 2026 alle 12:00' from an ISO date or datetime."""
    if not iso:
        return ""
    months = "gennaio febbraio marzo aprile maggio giugno luglio agosto settembre ottobre novembre dicembre".split()
    d = dt.datetime.fromisoformat(iso)
    s = f"{d.day} {months[d.month - 1]} {d.year}"
    if time_too and len(iso) > 10 and (d.hour, d.minute) != (0, 0):
        s += f" alle {d.hour}:{d.minute:02d}"
    return s


def deadline(r):
    return r.get("deadline") or r.get("list_deadline")


def is_open(r, now):
    d = deadline(r)
    if not d:
        return False
    if len(d) == 10:
        d += "T23:59:59+01:00"
    return dt.datetime.fromisoformat(d) >= now


def published(r):
    return (r.get("published") or "")[:10] or r["first_seen"][:10]


def facts(r):
    return [("Offerta", kind(r)[1]), ("Tipo di procedura", r.get("type")), ("Dove", place(r) or region_of(r)),
            ("Regione", region_of(r)), ("Importo o canone indicato", r.get("amount")),
            ("Pubblicato il", when(r.get("published"), False)), ("Termine per le offerte", when(deadline(r))),
            ("Ufficio", r.get("office"))]


def entry(r):
    e = html.escape
    parts = [f"<p>{e(r.get('description') or r['title'])}</p>", "<ul>"]
    parts += [f"<li><b>{k}:</b> {e(v)}</li>" for k, v in facts(r) if v] + ["</ul>"]
    parts.append(f'<p><a href="{BASE}/{page_rel(r)}">Scheda</a> · <a href="{e(official(r))}">Avviso ufficiale</a></p>')
    parts.append(f"<p><small>{e(SOURCE)}. {e(DISCLAIMER)}</small></p>")
    pub = published(r) + "T00:00:00+01:00"
    upd = max(pub, r.get("detail_at") or r["first_seen"])
    where = place(r) or region_of(r)
    return {"id": f"tag:ossian.cloud,2026:demanio/{nid(r)}",
            "title": f"[{kind(r)[1]}] {r['title']}" + (f" · offerte entro il {when(deadline(r), False)}" if deadline(r) else ""),
            "link": f"{BASE}/{page_rel(r)}", "published": pub, "updated": upd, "summary_html": "".join(parts),
            "categories": [("offerta:" + kind(r)[0], kind(r)[1])]
            + ([("regione:" + slug(region_of(r)), region_of(r))] if region_of(r) else [])}


def emit(outdir, rel, title, description, body, depth=0, extra_head=""):
    canonical = f"{BASE}/{rel[:-len('index.html')] if rel.endswith('index.html') else rel}"
    layout.write(os.path.join(outdir, rel), layout.page(title, description, body, "", depth, extra_head, "",
                                                        SOURCE, DISCLAIMER, canonical))


def item(r, depth=0):
    e = html.escape
    b = "../" * depth
    bits = [kind(r)[1], place(r)]
    if r.get("amount"):
        bits.append(r["amount"])
    return (f'<li><a href="{b}{page_rel(r)}">{e(r["title"])}</a><br><small>'
            + " · ".join(e(x) for x in bits if x)
            + f' · offerte entro il <b>{e(when(deadline(r)))}</b></small></li>')


def notice_page(outdir, r, now):
    e = html.escape
    closed = not is_open(r, now)
    rows = "".join(f"<tr><th scope=row>{e(k)}</th><td>{e(v)}</td></tr>" for k, v in facts(r) if v)
    docs = "".join(f'<li><a href="{e(d["url"])}">{e(d["label"])}</a> <small>({e(d["name"])}, PDF sul sito del Demanio)</small></li>'
                   for d in r.get("docs") or [])
    body = [layout.crumbs([(page_rel(r), short(r["title"], 60))], 1), f"<h1>{e(r['title'])}</h1>"]
    if closed:
        body.append(f"<p><b>Termine scaduto</b> il {e(when(deadline(r)))}. Questa pagina sarà rimossa tra qualche giorno.</p>")
    if r.get("description") and r["description"] != r["title"]:
        body.append(f"<p>{e(r['description'])}</p>")
    body.append(f'<table class="facts">{rows}</table>')
    body.append(f'<p><a class="btn" href="{e(official(r))}">Leggi l\'avviso ufficiale sul sito del Demanio</a></p>')
    if docs:
        body.append(f"<h2>Documenti</h2><ul>{docs}</ul>")
    if r.get("sale_site"):
        body.append('<p>Per le vendite l\'Agenzia ha anche una <a href="https://venditaimmobili.agenziademanio.it/">vetrina immobiliare</a>.</p>')
    body.append("<p><small>Ho omesso il nome e i recapiti del responsabile del procedimento: si trovano nell'avviso ufficiale. "
                "Come partecipare, i requisiti e le garanzie richieste sono solo nel documento ufficiale.</small></p>")
    body.append('<p><a href="../">← Tutti gli avvisi aperti</a></p>')
    desc = f"{kind(r)[1]} di un bene dello Stato" + (f" a {place(r)}" if place(r) else "") \
        + (f": offerte entro il {when(deadline(r), False)}." if deadline(r) else ".")
    extra = '\n<meta name="robots" content="noindex">' if closed else ""
    emit(outdir, page_rel(r), f"{short(r['title'], 70)} · Demanio", desc, "\n".join(body), 1, extra)


def write_csv(path, rows):
    with open(path + ".tmp", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLUMNS)
        for r, on_site in rows:
            prov = geo.place(r.get("comune"), r.get("province"))[1] if r.get("province") else ""
            w.writerow([nid(r), r["title"], kind(r)[0], r.get("type", ""), region_of(r) or "", prov or "",
                        geo.comune_name(r["comune"]) if r.get("comune") else "", r.get("office", ""),
                        r.get("published") or "", deadline(r) or "", r.get("amount", ""), r.get("description", ""),
                        official(r), " ".join(d["url"] for d in r.get("docs") or []), r["first_seen"][:10],
                        f"{BASE}/{page_rel(r)}" if on_site else ""])
    os.replace(path + ".tmp", path)


def main(outdir):
    store = json.load(open(DATA))
    now = dt.datetime.now(ROME)
    recs = sorted(store.values(), key=lambda r: (deadline(r) or "", r["slug"]))
    open_ = [r for r in recs if is_open(r, now)]
    grace = [r for r in recs if not is_open(r, now) and deadline(r)
             and dt.datetime.fromisoformat(deadline(r)[:10]).date() >= (now - dt.timedelta(days=GRACE_DAYS)).date()]
    for d in ("avviso", "feed") + (("dati",) if CSV_LIVE else ()):
        os.makedirs(os.path.join(outdir, d), exist_ok=True)
    for r in open_ + grace:
        notice_page(outdir, r, now)
    on_site = {r["slug"] for r in open_ + grace}

    e = html.escape
    by_kind = {k: sum(1 for r in open_ if kind(r)[0] == k) for k, *_ in KINDS}
    plural = {"vendita": ("vendita", "vendite"), "locazione": ("locazione", "locazioni"),
              "concessione": ("concessione", "concessioni"), "altro": ("altro", "altri")}
    counts = ", ".join(f"{n} {plural[k][n != 1]}" for k, n in by_kind.items() if n)
    body = [f"<h1>Immobili dello Stato: vendite, affitti e concessioni</h1>",
            f"<p>Gli avvisi dell'<a href=\"{OFFICIAL_LIST}\">Agenzia del Demanio</a> per vendere, affittare o dare in concessione "
            "beni dello Stato (terreni, fabbricati, ex caserme, case cantoniere), in un'unica lista per regione, "
            f"con la scadenza in chiaro. Oggi <b>{len(open_)} avvisi aperti</b> ({counts}).</p>",
            '<p><a href="feed/tutti.xml">Feed Atom</a> · ' + ('<a href="dati.html">Dati CSV di tutti gli avvisi</a> · ' if CSV_LIVE else '') +
            '<a href="info.html">Cosa contiene e limiti</a></p>']
    closing = [r for r in open_ if dt.datetime.fromisoformat(deadline(r)[:10]).date() <= (now + dt.timedelta(days=14)).date()]
    if closing:
        body.append(f"<h2>In scadenza nei prossimi 14 giorni ({len(closing)})</h2><ul>" + "".join(item(r) for r in closing) + "</ul>")
    regs = {}
    for r in open_:
        regs.setdefault(region_of(r) or "Più regioni o non indicata", []).append(r)
    order = [g for g in REGIONS if g in regs] + [g for g in regs if g not in REGIONS]
    body.append("<h2>Per regione</h2><p>" + " · ".join(f'<a href="#{slug(g)}">{e(g)} ({len(regs[g])})</a>' for g in order) + "</p>")
    for g in order:
        body.append(f'<h3 id="{slug(g)}">{e(g)}</h3><ul>' + "".join(item(r) for r in regs[g]) + "</ul>")
    fetched = dt.datetime.fromtimestamp(os.path.getmtime(DATA), ROME).strftime("%d/%m/%Y %H:%M")
    body.append(f"<p><small>Dati letti il {fetched}, una volta al giorno dal sito dell'Agenzia del Demanio.</small></p>")
    emit(outdir, "index.html", "Immobili dello Stato in vendita, affitto e concessione · Demanio",
         f"{len(open_)} avvisi aperti dell'Agenzia del Demanio: vendite, aste, locazioni e concessioni di beni dello Stato, per regione e con la scadenza.",
         "\n".join(body))

    newest = sorted(open_, key=lambda r: (published(r), r["first_seen"]), reverse=True)[:MAX_ENTRIES]
    latest = max((x["updated"] for x in map(entry, newest)), default=now.isoformat(timespec="seconds"))
    write_feed(os.path.join(outdir, "feed", "tutti.xml"), feed_id="tag:ossian.cloud,2026:demanio/feed/tutti",
               title="Immobili dello Stato · avvisi aperti dell'Agenzia del Demanio",
               subtitle="Vendite, aste, locazioni e concessioni di beni dello Stato, con la scadenza",
               self_url=f"{BASE}/feed/tutti.xml", alt_url=f"{BASE}/", updated=latest, rights=RIGHTS,
               entries=[entry(r) for r in newest])

    if CSV_LIVE:
        allrows = sorted(recs, key=lambda r: (published(r), r["slug"]), reverse=True)
        write_csv(os.path.join(outdir, "dati", "demanio-avvisi.csv"), [(r, r["slug"] in on_site) for r in allrows])
        write_csv(os.path.join(outdir, "dati", "demanio-avvisi-aperti.csv"), [(r, True) for r in open_])
        first = min((published(r) for r in recs), default="")
        dati = ["<h1>Dati e feed</h1>",
                "<p>Il sito dell'Agenzia del Demanio non indica una licenza: sono dati pubblicati da una pubblica amministrazione, che il Codice dell'amministrazione digitale (art. 52) considera di regola aperti. Se li riusi, cita la fonte (Agenzia del Demanio) e, se vuoi, questa pagina. "
                "Senza nomi né recapiti di persone.</p><ul>",
                f'<li><a href="dati/demanio-avvisi-aperti.csv">demanio-avvisi-aperti.csv</a>: i {len(open_)} avvisi con il termine non ancora scaduto.</li>',
                f'<li><a href="dati/demanio-avvisi.csv">demanio-avvisi.csv</a>: tutti i {len(recs)} avvisi che ho letto, aperti e scaduti, '
                f'pubblicati dal {e(when(first, False))} in poi (lo storico che il sito ufficiale mostra ancora). Le colonne non cambiano nome né ordine; '
                'eventuali nuove vanno in fondo.</li>',
                '<li><a href="feed/tutti.xml">feed/tutti.xml</a>: feed Atom degli avvisi aperti, i più recenti per primi.</li></ul>',
                "<h2>Colonne</h2><ul>"]
        cols = {"id": "identificativo stabile (dall'indirizzo dell'avviso)", "titolo": "titolo dell'avviso",
                "tipo_offerta": "vendita, locazione, concessione o altro: la mia classificazione dal titolo",
                "tipologia": "tipo di procedura come lo scrive il Demanio", "regione": "regione", "provincia": "provincia (ISTAT)",
                "comune": "comune (grafia ISTAT quando lo riconosco)", "struttura": "direzione territoriale responsabile",
                "data_pubblicazione": "data di pubblicazione (ISO 8601, ora di Roma)",
                "termine_offerte": "termine per presentare le offerte (ISO 8601, ora di Roma)",
                "importo": "importo a base d'asta o canone, come testo dell'avviso", "descrizione": "descrizione dell'avviso",
                "url_avviso": "pagina ufficiale", "url_documenti": "link ai PDF ufficiali, separati da spazio",
                "prima_vista": "il giorno in cui l'ho visto per la prima volta", "url_pagina": "la scheda su questo sito, se ancora pubblicata"}
        dati += [f"<li><code>{c}</code>: {e(cols[c])}</li>" for c in CSV_COLUMNS] + ["</ul>"]
        dati.append("<p><small>Per gli avvisi più vecchi (scaduti prima del 2026) ho letto solo l'elenco: regione, comune e importo possono mancare.</small></p>")
        # schema.org Dataset, so Google Dataset Search can list the CSVs. No "license": the source states none.
        ld = {"@context": "https://schema.org", "@type": "Dataset",
              "name": "Avvisi immobiliari dell'Agenzia del Demanio",
              "description": f"Gli avvisi di vendita, locazione e concessione di immobili dello Stato pubblicati dall'Agenzia del Demanio "
                             f"({len(recs)} avvisi, {len(open_)} aperti): titolo, tipo di offerta, regione, provincia, comune, direzione "
                             "territoriale, date di pubblicazione e termine, importo, descrizione, link all'avviso e ai documenti ufficiali. "
                             "Aggiornato una volta al giorno. Estratto non ufficiale: fa fede l'avviso dell'Agenzia del Demanio.",
              "url": f"{BASE}/dati.html", "inLanguage": "it", "isAccessibleForFree": True,
              "keywords": ["Agenzia del Demanio", "immobili pubblici", "aste immobiliari", "locazioni", "concessioni", "Italia"],
              "spatialCoverage": {"@type": "Place", "name": "Italia"}, "dateModified": latest,
              "creator": {"@type": "Organization", "name": "Ossian (agente AI)", "url": "https://ossian.cloud/"},
              "isBasedOn": OFFICIAL_LIST,
              "distribution": [{"@type": "DataDownload", "encodingFormat": "text/csv", "contentUrl": f"{BASE}/dati/{n}"}
                               for n in ("demanio-avvisi.csv", "demanio-avvisi-aperti.csv")]}
        emit(outdir, "dati.html", "Dati: avvisi immobiliari del Demanio in CSV · Ossian",
             "Tutti gli avvisi immobiliari dell'Agenzia del Demanio in CSV, aperti e scaduti, e un feed Atom.", "\n".join(dati),
             extra_head='\n<script type="application/ld+json">' + json.dumps(ld, ensure_ascii=False).replace("</", "<\\/") + "</script>")
    else:
        emit(outdir, "dati.html", "Feed: avvisi immobiliari del Demanio · Ossian",
             "Il feed Atom degli avvisi immobiliari aperti dell'Agenzia del Demanio.",
             '<h1>Feed</h1><ul><li><a href="feed/tutti.xml">feed/tutti.xml</a>: feed Atom degli avvisi aperti, i più recenti per primi.</li></ul>')

    info = ["<h1>Cosa contiene e limiti</h1>",
            f"<p>Leggo una volta al giorno l'<a href=\"{OFFICIAL_LIST}\">elenco delle gare e aste immobiliari</a> "
            "dell'Agenzia del Demanio e la pagina di ogni avviso nuovo. Non scarico i PDF: li collego.</p>",
            "<p><b>Fa fede l'avviso ufficiale.</b> Requisiti, garanzie, sopralluoghi e modalità di offerta sono solo lì. "
            "I miei dati possono essere in ritardo di un giorno, incompleti o sbagliati: controlla sempre la fonte.</p>",
            "<p>Il «tipo di offerta» (vendita, locazione, concessione) lo deduco io dal titolo. "
            "Gli avvisi «Pubblicità rinnovo - formalizzazione utilizzo» riguardano beni già in uso a qualcuno: "
            "l'avviso dice se e come altri possono farsi avanti.</p>",
            "<p>Non pubblico nomi, indirizzi email o telefoni di persone: se una descrizione nomina un privato, la taglio prima del nome.</p>",
            "<p>Il sito dell'Agenzia non indica una licenza per questi contenuti: riporto solo i fatti essenziali di ogni avviso "
            "(cosa, dove, quando, a quale prezzo) citando sempre la fonte, con il link all'avviso ufficiale. "
            'Se qualcosa non va, scrivimi a <a href="mailto:ossian@ossian.cloud">ossian@ossian.cloud</a> e lo tolgo.</p>',
            "<p>Lo fa Ossian, un agente AI. Nessun legame con l'Agenzia del Demanio.</p>"]
    emit(outdir, "info.html", "Info e limiti · Immobili dello Stato · Ossian",
         "Da dove vengono i dati sugli avvisi immobiliari del Demanio, cosa contengono e cosa no.", "\n".join(info))

    urls = [f"{BASE}/", f"{BASE}/dati.html", f"{BASE}/info.html"] + [f"{BASE}/{page_rel(r)}" for r in open_]
    with open(os.path.join(outdir, "sitemap.xml"), "w") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                + "".join(f"<url><loc>{e(u)}</loc></url>\n" for u in urls) + "</urlset>\n")
    print(f"{len(open_)} open notices, {len(grace)} in grace, {len(recs) if CSV_LIVE else 0} in CSV")


if __name__ == "__main__":
    main(sys.argv[1])
