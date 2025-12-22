import json
import time
import re
import os
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime

from openai import OpenAI

INPUT_JSON = "data/relevant_items.json"
OUTPUT_RSS = "docs/betterdoi.xml"
PUBMED_LOG = "data/pubmed_raw.txt"

CROSSREF_API = "https://api.crossref.org/works"
PUBMED_ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

USER_AGENT = "sentinelnode/1.0 (mailto:colmmemedsurv@users.noreply.github.com)"
RATE_LIMIT = 0.4
MODEL = "gpt-4.1-mini"

DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.I)


# ---------------- Utilities ----------------

def xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
         .replace("'", "&apos;")
    )


def norm_title(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"[^\w\s]", "", s)
    return re.sub(r"\s+", " ", s).strip()


# ---------------- Crossref ----------------

def crossref_search(title: str, rows=5):
    try:
        r = requests.get(
            CROSSREF_API,
            params={"query.title": title, "rows": rows},
            headers={"User-Agent": USER_AGENT},
            timeout=20,
        )
        if r.status_code != 200:
            return []
        return r.json().get("message", {}).get("items", [])
    except Exception:
        return []


# ---------------- OpenAI comparator ----------------

def same_article(client, a_title, a_authors, b_title, b_authors):
    prompt = f"""Do these refer to the SAME journal article?
Answer YES or NO only.

A:
Title: {a_title}
Authors: {", ".join(a_authors)}

B:
Title: {b_title}
Authors: {", ".join(b_authors)}
"""
    r = client.responses.create(model=MODEL, input=prompt)
    return (r.output_text or "").strip().upper() == "YES"


def recover_doi(item, client):
    title = item.get("title", "")
    authors = item.get("authors", [])
    wanted = norm_title(title)

    for c in crossref_search(title):
        ct = (c.get("title") or [""])[0]
        if norm_title(ct) == wanted:
            return c.get("DOI")

    for c in crossref_search(title):
        ct = (c.get("title") or [""])[0]
        ca = [
            " ".join(filter(None, [a.get("given"), a.get("family")]))
            for a in c.get("author", [])
        ]
        if same_article(client, title, authors, ct, ca):
            return c.get("DOI")

    return None


# ---------------- PubMed ----------------

def pubmed_fetch_abstract(doi: str):
    try:
        s = requests.get(
            PUBMED_ESEARCH,
            params={"db": "pubmed", "term": f"{doi}[DOI]", "retmode": "json"},
            headers={"User-Agent": USER_AGENT},
            timeout=20,
        )
        time.sleep(RATE_LIMIT)
        pmids = s.json()["esearchresult"]["idlist"]
        if not pmids:
            return None

        f = requests.get(
            PUBMED_EFETCH,
            params={"db": "pubmed", "id": pmids[0], "retmode": "xml"},
            headers={"User-Agent": USER_AGENT},
            timeout=20,
        )

        with open(PUBMED_LOG, "a", encoding="utf-8") as log:
            log.write(f"\n===== DOI {doi} =====\n")
            log.write(f.text)

        root = ET.fromstring(f.text)
        abs_el = root.find(".//AbstractText")
        if abs_el is not None and abs_el.text:
            return abs_el.text.strip()

    except Exception:
        return None

    return None


# ---------------- RSS ----------------

def build_rss(items):
    now = format_datetime(datetime.now(timezone.utc))
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:prism="http://prismstandard.org/namespaces/basic/2.0/">',
        "<channel>",
        "<title>Head &amp; Neck Cancer – DOI-Enriched Feed</title>",
        "<link>https://colmmemedsurv.github.io/sentinelnode/</link>",
        "<description>Curated head &amp; neck cancer literature with DOI, Crossref and PubMed enrichment.</description>",
        f"<lastBuildDate>{xml_escape(now)}</lastBuildDate>",
    ]

    for it in items:
        out.append("<item>")
        out.append(f"<title>{xml_escape(it.get('title',''))}</title>")
        if it.get("link"):
            out.append(f"<link>{xml_escape(it['link'])}</link>")
            out.append(f"<guid isPermaLink='true'>{xml_escape(it['link'])}</guid>")

        if it.get("authors"):
            out.append(f"<dc:creator>{xml_escape('; '.join(it['authors']))}</dc:creator>")
        if it.get("journal"):
            out.append(f"<prism:publicationName>{xml_escape(it['journal'])}</prism:publicationName>")
        if it.get("doi"):
            out.append(f"<prism:doi>{xml_escape(it['doi'])}</prism:doi>")

        desc = f"Journal: {it.get('journal','')} | DOI: {it.get('doi','')}"
        out.append(f"<description>{xml_escape(desc)}</description>")

        out.append("<content:encoded><![CDATA[")
        out.append(f"<p><strong>Journal</strong>: {it.get('journal','')}</p>")
        if it.get("authors"):
            out.append(f"<p><strong>Authors</strong>: {'; '.join(it['authors'])}</p>")
        if it.get("doi"):
            out.append(f"<p><strong>DOI</strong>: <a href='https://doi.org/{it['doi']}'>{it['doi']}</a></p>")
        out.append("<hr/>")
        out.append("<p><strong>Abstract</strong></p>")
        out.append(f"<p>{it.get('abstract','Abstract not available')}</p>")
        out.append("]]></content:encoded>")
        out.append("</item>")

    out.append("</channel></rss>")
    return "\n".join(out)


# ---------------- Main ----------------

def main():
    with open(INPUT_JSON, "r", encoding="utf-8") as f:
        items = json.load(f)

    client = OpenAI()
    enriched = []

    for it in items:
        it = dict(it)

        if it.get("allow_doi_lookup"):
            if not it.get("doi"):
                it["doi"] = recover_doi(it, client)
                time.sleep(RATE_LIMIT)

            if it.get("doi"):
                it["abstract"] = pubmed_fetch_abstract(it["doi"])
                time.sleep(RATE_LIMIT)

        enriched.append(it)

    with open(OUTPUT_RSS, "w", encoding="utf-8") as f:
        f.write(build_rss(enriched))


if __name__ == "__main__":
    main()
