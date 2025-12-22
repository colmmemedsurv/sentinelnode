import json
import time
import re
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime

INPUT_JSON = "data/relevant_items.json"
OUTPUT_RSS = "docs/betterdoi.xml"
PUBMED_DEBUG = "docs/pubmed_raw.txt"

CROSSREF_API = "https://api.crossref.org/works/"
PUBMED_ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

USER_AGENT = "sentinelnode/1.0 (mailto:colmmemedsurv@users.noreply.github.com)"
RATE_LIMIT_SLEEP = 0.34


# ---------------- Utilities ----------------

def xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
         .replace("'", "&apos;")
    )


def strip_xml_tags(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


# ---------------- Crossref ----------------

def crossref_lookup(doi: str) -> dict | None:
    try:
        r = requests.get(
            CROSSREF_API + doi,
            headers={"User-Agent": USER_AGENT},
            timeout=20,
        )
        if r.status_code != 200:
            return None
        return r.json().get("message")
    except Exception:
        return None


# ---------------- PubMed ----------------

def pubmed_lookup_by_doi(doi: str, debug_log: list[str]) -> dict | None:
    try:
        # Step 1: DOI -> PMID
        r = requests.get(
            PUBMED_ESEARCH,
            params={
                "db": "pubmed",
                "term": doi,
                "retmode": "json",
            },
            timeout=20,
        )
        ids = r.json().get("esearchresult", {}).get("idlist", [])
        if not ids:
            debug_log.append(f"DOI {doi}: No PMID found\n")
            return None

        pmid = ids[0]

        # Step 2: Fetch full record
        r = requests.get(
            PUBMED_EFETCH,
            params={
                "db": "pubmed",
                "id": pmid,
                "retmode": "xml",
            },
            timeout=20,
        )

        debug_log.append(f"\n===== DOI {doi} =====\n{r.text}\n")

        root = ET.fromstring(r.text)
        article = root.find(".//Article")
        if article is None:
            return None

        # Abstract
        abstract_parts = article.findall(".//AbstractText")
        abstract = " ".join(
            strip_xml_tags(ET.tostring(a, encoding="unicode"))
            for a in abstract_parts
        ).strip()

        # Journal
        journal = article.findtext(".//Journal/Title")

        # Pub date (safe)
        year = article.findtext(".//PubDate/Year")
        month = article.findtext(".//PubDate/Month") or "1"
        day = article.findtext(".//PubDate/Day") or "1"

        try:
            dt = datetime(int(year), int(month), int(day), tzinfo=timezone.utc)
            pub_date = format_datetime(dt)
        except Exception:
            pub_date = None

        return {
            "abstract": abstract or "Abstract not available",
            "journal": journal,
            "pubDate": pub_date,
        }

    except Exception as e:
        debug_log.append(f"DOI {doi}: ERROR {e}\n")
        return None


# ---------------- RSS ----------------

def build_rss(items: list[dict]) -> str:
    now = format_datetime(datetime.now(timezone.utc))
    parts = [
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

        if it.get("doi"):
            parts.append(f"<prism:doi>{xml_escape(it['doi'])}</prism:doi>")

        desc = "Journal: {} | DOI: {}".format(it.get("journal",""), it.get("doi",""))
        parts.append(f"<description>{xml_escape(desc)}</description>")

        parts.append("<content:encoded><![CDATA[")
        parts.append(f"<p><strong>Journal</strong>: {it.get('journal','')}</p>")
        if it.get("authors"):
            parts.append(f"<p><strong>Authors</strong>: {'; '.join(it['authors'])}</p>")
        if it.get("doi"):
            parts.append(f"<p><strong>DOI</strong>: <a href='https://doi.org/{it['doi']}'>{it['doi']}</a></p>")
        parts.append("<hr/>")
        parts.append("<p><strong>Abstract</strong></p>")
        parts.append(f"<p>{it.get('abstract','Abstract not available')}</p>")
        parts.append("]]></content:encoded>")
        parts.append("</item>")

    parts.append("</channel></rss>")
    return "\n".join(parts)


# ---------------- Main ----------------

def main():
    with open(INPUT_JSON, "r", encoding="utf-8") as f:
        items = json.load(f)

    enriched = []
    pubmed_debug = []

    for it in items:
        it = dict(it)
        doi = (it.get("doi") or "").strip()

        if doi:
            # Crossref
            cr = crossref_lookup(doi)
            if cr:
                if not it.get("journal") and cr.get("container-title"):
                    it["journal"] = cr["container-title"][0]
                if not it.get("authors") and cr.get("author"):
                    it["authors"] = [
                        " ".join(filter(None, [a.get("given"), a.get("family")]))
                        for a in cr["author"]
                    ]

            # PubMed
            pm = pubmed_lookup_by_doi(doi, pubmed_debug)
            if pm:
                it.setdefault("abstract", pm.get("abstract"))
                it.setdefault("journal", pm.get("journal"))
                it.setdefault("pubDate", pm.get("pubDate"))

            time.sleep(RATE_LIMIT_SLEEP)

        it["doi"] = doi
        it.setdefault("abstract", "Abstract not available")
        enriched.append(it)

    with open(PUBMED_DEBUG, "w", encoding="utf-8") as f:
        f.write("\n".join(pubmed_debug))

    rss = build_rss(enriched)
    with open(OUTPUT_RSS, "w", encoding="utf-8") as f:
        f.write(rss)


if __name__ == "__main__":
    main()
