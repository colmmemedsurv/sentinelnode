#!/usr/bin/env python3

import feedparser
import requests
import time
import xml.etree.ElementTree as ET
from html import escape as xml_escape
from email.utils import formatdate
from pathlib import Path

FEEDS_FILE = "feeds.txt"
OUTPUT_FILE = "docs/betterdoi.xml"
PUBMED_DEBUG_FILE = "docs/pubmed_raw.txt"

PUBMED_ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

HEADERS = {"User-Agent": "sentinelnode/1.0"}

PUBMED_SLEEP = 0.5  # be nice to NCBI


# ----------------------------
# Helpers
# ----------------------------

def load_feeds():
    feeds = []
    allow_lookup = set()

    with open(FEEDS_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("[ALLOW_DOI_LOOKUP]"):
                url = line.replace("[ALLOW_DOI_LOOKUP]", "").strip()
                feeds.append(url)
                allow_lookup.add(url)
            else:
                feeds.append(line)

    return feeds, allow_lookup


def extract_doi(entry):
    # ScienceDirect puts DOI in prism:doi
    if "prism_doi" in entry:
        return entry.prism_doi.strip()
    if "doi" in entry:
        return entry.doi.strip()
    return None


# ----------------------------
# PubMed
# ----------------------------

def pubmed_lookup_by_doi(doi, debug_fh):
    debug_fh.write(f"\n===== DOI {doi} =====\n")

    # --- esearch ---
    params = {
        "db": "pubmed",
        "term": doi,
        "retmode": "xml",
    }
    r = requests.get(PUBMED_ESEARCH, params=params, headers=HEADERS)
    debug_fh.write(r.text + "\n")

    if r.status_code != 200:
        return None

    try:
        root = ET.fromstring(r.text)
    except ET.ParseError:
        return None

    id_list = root.find("IdList")
    if id_list is None or len(id_list) == 0:
        return None

    pmid = id_list.find("Id").text.strip()

    time.sleep(PUBMED_SLEEP)

    # --- efetch ---
    params = {
        "db": "pubmed",
        "id": pmid,
        "retmode": "xml",
    }
    r = requests.get(PUBMED_EFETCH, params=params, headers=HEADERS)
    debug_fh.write(r.text + "\n")

    if r.status_code != 200:
        return None

    try:
        root = ET.fromstring(r.text)
    except ET.ParseError:
        return None

    article = root.find(".//Article")
    if article is None:
        return None

    # --- Abstract (concatenate all sections) ---
    abstract_parts = []
    for abs_text in article.findall(".//AbstractText"):
        label = abs_text.attrib.get("Label")
        text = abs_text.text or ""
        if label:
            abstract_parts.append(f"{label}: {text}")
        else:
            abstract_parts.append(text)

    abstract = "\n\n".join(abstract_parts).strip()
    if not abstract:
        abstract = "Abstract not available"

    # --- Authors ---
    authors = []
    for a in article.findall(".//Author"):
        last = a.findtext("LastName")
        fore = a.findtext("ForeName")
        if last and fore:
            authors.append(f"{fore} {last}")

    # --- Journal ---
    journal = article.findtext(".//Journal/Title") or ""

    return {
        "abstract": abstract,
        "authors": ", ".join(authors),
        "journal": journal,
        "pmid": pmid,
    }


# ----------------------------
# Main
# ----------------------------

def main():
    feeds, allow_lookup = load_feeds()

    items = []
    debug_fh = open(PUBMED_DEBUG_FILE, "w", encoding="utf-8")

    for feed_url in feeds:
        feed = feedparser.parse(feed_url)

        for entry in feed.entries:
            item = {
                "title": entry.get("title", ""),
                "link": entry.get("link", ""),
                "journal": entry.get("prism_publicationname", ""),
                "authors": "",
                "doi": "",
                "abstract": "Abstract not available",
                "pubdate": entry.get("published_parsed"),
            }

            doi = extract_doi(entry)
            if doi:
                item["doi"] = doi

            # Only PubMed lookup for allowed feeds
            if feed_url in allow_lookup and doi:
                data = pubmed_lookup_by_doi(doi, debug_fh)
                if data:
                    item["abstract"] = data["abstract"]
                    item["authors"] = data["authors"]
                    if data["journal"]:
                        item["journal"] = data["journal"]

            items.append(item)

    debug_fh.close()

    # ----------------------------
    # Write RSS
    # ----------------------------
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write('<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" '
                'xmlns:dc="http://purl.org/dc/elements/1.1/" '
                'xmlns:prism="http://prismstandard.org/namespaces/basic/2.0/">\n')
        f.write("<channel>\n")
        f.write("<title>Head &amp; Neck Cancer – DOI-Enriched Feed</title>\n")
        f.write("<link>https://colmmemedsurv.github.io/sentinelnode/</link>\n")
        f.write("<description>Curated head &amp; neck cancer literature with PubMed enrichment.</description>\n")
        f.write(f"<lastBuildDate>{formatdate()}</lastBuildDate>\n")

        for it in items:
            f.write("<item>\n")
            f.write(f"<title>{xml_escape(it['title'])}</title>\n")
            f.write(f"<link>{xml_escape(it['link'])}</link>\n")
            f.write(f"<guid isPermaLink='true'>{xml_escape(it['link'])}</guid>\n")

            if it["authors"]:
                f.write(f"<dc:creator>{xml_escape(it['authors'])}</dc:creator>\n")

            if it["journal"]:
                f.write(f"<prism:publicationName>{xml_escape(it['journal'])}</prism:publicationName>\n")

            if it["doi"]:
                f.write(f"<prism:doi>{xml_escape(it['doi'])}</prism:doi>\n")

            desc = "Journal: " + it["journal"]
            if it["doi"]:
                desc += " | DOI: " + it["doi"]
            f.write(f"<description>{xml_escape(desc)}</description>\n")

            f.write("<content:encoded><![CDATA[\n")
            if it["journal"]:
                f.write(f"<p><strong>Journal</strong>: {xml_escape(it['journal'])}</p>\n")
            if it["authors"]:
                f.write(f"<p><strong>Authors</strong>: {xml_escape(it['authors'])}</p>\n")
            if it["doi"]:
                doi_url = "https://doi.org/" + it["doi"]
                f.write(f"<p><strong>DOI</strong>: <a href='{doi_url}'>{it['doi']}</a></p>\n")

            f.write("<hr/>\n")
            f.write("<p><strong>Abstract</strong></p>\n")
            f.write(f"<p>{xml_escape(it['abstract']).replace(chr(10), '<br/>')}</p>\n")
            f.write("]]></content:encoded>\n")

            f.write("</item>\n")

        f.write("</channel></rss>\n")


if __name__ == "__main__":
    main()
