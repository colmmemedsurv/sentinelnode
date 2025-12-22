import json
import time
import re
from datetime import datetime, timezone
from email.utils import format_datetime
import requests

# ---------------- CONFIG ----------------

INPUT_JSON = "data/relevant_items.json"
OUTPUT_RSS = "docs/betterdoi.xml"

CROSSREF_API = "https://api.crossref.org/works"
PUBMED_SEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_FETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

USER_AGENT = "sentinelnode/1.0 (mailto:colmmemedsurv@users.noreply.github.com)"
RATE_LIMIT_SLEEP = 0.25

DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.I)

# ---------------- HELPERS ----------------

def xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
         .replace("'", "&apos;")
    )

def strip_xml(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()

def norm_title(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"[^\w\s]", "", s)
    return re.sub(r"\s+", " ", s).strip()

def extract_doi(text: str | None) -> str | None:
    if not text:
        return None
    m = DOI_RE.search(text)
    return m.group(0) if m else None

# ---------------- CROSSREF ----------------

def crossref_lookup_doi(doi: str) -> dict | None:
    try:
        r = requests.get(
            f"{CROSSREF_API}/{doi}",
            headers={"User-Agent": USER_AGENT},
            timeout=20,
        )
        if r.status_code != 200:
            return None
        return r.json().get("message")
    except Exception:
        return None

def crossref_search_title(title: str, rows=5) -> list[dict]:
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

def recover_doi_from_title(item: dict) -> str | None:
    wanted = norm_title(item.get("title", ""))
    for cand in crossref_search_title(item.get("title", "")):
        cr_title = norm_title((cand.get("title") or [""])[0])
        if wanted == cr_title or wanted in cr_title or cr_title in wanted:
            return cand.get("DOI")
    return None

# ---------------- PUBMED ----------------

def pubmed_abstract_from_doi(doi: str) -> str | None:
    try:
        search = requests.get(
            PUBMED_SEARCH,
            params={
                "db": "pubmed",
                "term": doi,
                "retmode": "json"
            },
            timeout=20,
        ).json()

        ids = search.get("esearchresult", {}).get("idlist", [])
        if not ids:
            return None

        fetch = requests.get(
            PUBMED_FETCH,
            params={
                "db": "pubmed",
                "id": ids[0],
                "retmode": "xml"
            },
            timeout=20,
        ).text

        m = re.search(r"<AbstractText[^>]*>(.*?)</AbstractText>", fetch, re.S)
        if m:
            return strip_xml(m.group(1))

    except Exception:
        return None

    return None

# ---------------- RSS BUILDER ----------------

def build_rss(items: list[dict]) -> str:
    now = format_datetime(datetime.now(timezone.utc))

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" '
        'xmlns:content="http://purl.org/rss/1.0/modules/content/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:prism="http://prismstandard.org/namespaces/basic/2.0/">',
        "<channel>",
        "<title>Head &amp; Neck Cancer – DOI-Enriched Feed</title>",
        "<link>https://colmmemedsurv.github.io/sentinelnode/</link>",
        "<description>Curated head &amp; neck cancer literature with DOI, Crossref and PubMed enrichment.</description>",
        f"<lastBuildDate>{xml_escape(now)}</lastBuildDate>",
    ]

    for it in items:
        parts.append("<item>")
        parts.append(f"<title>{xml_escape(it.get('title',''))}</title>")

        if it.get("link"):
            parts.append(f"<link>{xml_escape(it['link'])}</link>")
            parts.append(f"<guid isPermaLink='true'>{xml_escape(it['link'])}</guid>")

        if it.get("pubDate"):
            parts.append(f"<pubDate>{xml_escape(it['pubDate'])}</pubDate>")

        if it.get("authors"):
            parts.append(f"<dc:creator>{xml_escape('; '.join(it['authors']))}</dc:creator>")

        if it.get("journal"):
            parts.append(f"<prism:publicationName>{xml_escape(it['journal'])}</prism:publicationName>")

        if it.get("doi_display") and it["doi_display"] != "DOI not found":
            parts.append(f"<prism:doi>{xml_escape(it['doi_display'])}</prism:doi>")

        desc = f"Journal: {it.get('journal','')} | DOI: {it.get('doi_display','')}"
        parts.append(f"<description>{xml_escape(desc)}</description>")

        parts.append("<content:encoded><![CDATA[")

        parts.append(f"<p><strong>Journal</strong>: {it.get('journal','')}</p>")

        if it.get("authors"):
            parts.append(f"<p><strong>Authors</strong>: {'; '.join(it['authors'])}</p>")

        if it.get("doi_display") != "DOI not found":
            parts.append(
                f"<p><strong>DOI</strong>: "
                f"<a href='https://doi.org/{it['doi_display']}'>{it['doi_display']}</a></p>"
            )

        parts.append("<hr/>")
        parts.append("<p><strong>Abstract</strong></p>")
        parts.append(f"<p>{it.get('abstract','Abstract not available')}</p>")

        parts.append("]]></content:encoded>")
        parts.append("</item>")

    parts.append("</channel></rss>")
    return "\n".join(parts)

# ---------------- MAIN ----------------

def main():
    with open(INPUT_JSON, "r", encoding="utf-8") as f:
        items = json.load(f)

    enriched = []

    for it in items:
        it = dict(it)

        doi = extract_doi(it.get("doi"))
        if not doi and it.get("allow_doi_lookup"):
            doi = recover_doi_from_title(it)
            time.sleep(RATE_LIMIT_SLEEP)

        it["doi_display"] = doi if doi else "DOI not found"

        cr = crossref_lookup_doi(doi) if doi else None
        if cr:
            time.sleep(RATE_LIMIT_SLEEP)

            it.setdefault("journal", (cr.get("container-title") or [""])[0])

            if not it.get("authors") and cr.get("author"):
                it["authors"] = [
                    " ".join(filter(None, [a.get("given"), a.get("family")]))
                    for a in cr["author"]
                ]

            if not it.get("abstract") and cr.get("abstract"):
                it["abstract"] = strip_xml(cr["abstract"])

            if not it.get("link"):
                it["link"] = cr.get("URL") or f"https://doi.org/{doi}"

            issued = cr.get("issued", {}).get("date-parts")
            if issued:
                y, m, d = (issued[0] + [1, 1])[:3]
                dt = datetime(y, m, d, tzinfo=timezone.utc)
                it["pubDate"] = format_datetime(dt)

        if doi and not it.get("abstract"):
            pm_abs = pubmed_abstract_from_doi(doi)
            if pm_abs:
                it["abstract"] = pm_abs

        if not it.get("abstract"):
            it["abstract"] = "Abstract not available"

        enriched.append(it)

    rss = build_rss(enriched)
    with open(OUTPUT_RSS, "w", encoding="utf-8") as f:
        f.write(rss)

if __name__ == "__main__":
    main()
