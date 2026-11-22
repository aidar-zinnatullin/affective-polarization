"""Bibliographic search, journal lookup, and open full-text retrieval."""
import csv
from difflib import SequenceMatcher
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from xml.etree import ElementTree as ET
from agents import utcdate

START_DATE = "2025-08-23"
CAUSE_TERMS = "(causes OR predictors OR drivers OR roots OR origins OR determinants)"
SEARCHES = {"Primary": '("affective polarization" OR "affective polarisation")',
            "Negative partisanship": '"negative partisanship"',
            "Hostility toward the opposition party": '"hostility toward the opposition party"',
            "Partisan animosity": '"partisan animosity"'}


class Retrieval(Exception):
    pass


def fetch(url, limit=12_000_000, timeout=25):
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "AffectivePolarizationReview/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as response:
                data = response.read(limit + 1)
            if len(data) > limit:
                raise Retrieval("Service response exceeds the size limit")
            return data
        except urllib.error.HTTPError as e:
            if e.code in {429, 502, 503, 504} and attempt < 2:
                time.sleep(2 ** (attempt + 1))
                continue
            raise Retrieval("HTTP " + str(e.code)) from None
        except OSError:
            if attempt < 2:
                time.sleep(1 + attempt)
                continue
            raise Retrieval("Network request failed") from None


def oa_json(path, **params):
    params["api_key"] = os.environ["OPENALEX_API_KEY"]
    raw = fetch("https://api.openalex.org/" + path + "?" + urllib.parse.urlencode(params))
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError):
        raise Retrieval("Invalid OpenAlex metadata response") from None


def search(label):
    query = SEARCHES[label] + " AND " + CAUSE_TERMS
    params = {"search.title_and_abstract": query,
              "filter": f"from_publication_date:{START_DATE},to_publication_date:{utcdate()}",
              "per_page": 100, "cursor": "*",
              "select": "id,doi,display_name,publication_date,authorships,primary_location,type,language,is_retracted,has_content,abstract_inverted_index"}
    cursors = set()
    while True:
        if params["cursor"] in cursors:
            raise Retrieval("Repeated OpenAlex cursor")
        cursors.add(params["cursor"])
        page = oa_json("works", **params)
        if not isinstance(page.get("results"), list) or not isinstance(page.get("meta"), dict):
            raise Retrieval("Unexpected OpenAlex page shape")
        yield page["results"]
        cursor = page["meta"].get("next_cursor")
        if not page["results"] or not cursor:
            return
        params["cursor"] = cursor


def norm(value):
    stripped = unicodedata.normalize("NFKD", value or "").casefold()
    return " ".join(re.findall(r"[^\W_]+", stripped, re.UNICODE))


def doi(value):
    return re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", (value or "").strip(),
                  flags=re.I).casefold()


def article(item):
    loc = item.get("primary_location") or {}
    source = loc.get("source") or {}
    authors = [a.get("author", {}).get("display_name", "") for a in item.get("authorships", [])]
    wid = item["id"].rsplit("/", 1)[-1]
    if not re.fullmatch(r"W\d+", wid):
        raise Retrieval("Invalid OpenAlex work identifier")
    return {"id": wid, "title": item.get("display_name") or "", "doi": doi(item.get("doi")),
            "date": item.get("publication_date") or "", "authors": authors,
            "journal": source.get("display_name") or "", "issn": source.get("issn") or [],
            "source_type": source.get("type") or "", "type": item.get("type") or "",
            "language": item.get("language") or "", "retracted": bool(item.get("is_retracted")),
            "xml": bool((item.get("has_content") or {}).get("grobid_xml"))}


def abstract(item):
    index = item.get("abstract_inverted_index") or {}
    return " ".join(word for _, word in sorted((pos, word) for word, positions in index.items()
                                                for pos in positions))


def full_text(meta):
    work_id = meta["id"]
    key = urllib.parse.urlencode({"api_key": os.environ["OPENALEX_API_KEY"]})
    body = fetch("https://content.openalex.org/works/" + work_id + ".grobid-xml?" + key)
    if body.startswith(b"\x1f\x8b"):
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as compressed:
            body = compressed.read(12_000_001)
        if len(body) > 12_000_000:
            raise Retrieval("Full text exceeds the size limit")
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise Retrieval("OpenAlex full text could not be parsed") from None
    for elem in root.iter():
        elem.tag = elem.tag.split("}")[-1]
    source_title = root.find(".//teiHeader/fileDesc/titleStmt/title")
    title = "".join(source_title.itertext()) if source_title is not None else ""
    source_dois = {doi(e.text) for e in root.findall(".//teiHeader/fileDesc/sourceDesc//idno")
                   if e.attrib.get("type", "").casefold() == "doi" and e.text}
    if meta["doi"] and source_dois and meta["doi"] not in source_dois:
        raise Retrieval("Full-text DOI needs verification")
    matched_doi = bool(meta["doi"] and meta["doi"] in source_dois)
    if not matched_doi and SequenceMatcher(None, norm(title), norm(meta["title"])).ratio() < 0.73:
        raise Retrieval("Full-text identity needs verification")
    article_body = root.find(".//text/body")
    if article_body is None:
        raise Retrieval("No article body in the full text")
    sections = []
    def traverse(node):
        if node.tag in {"head", "p", "table", "figure"}:
            txt = " ".join(" ".join(node.itertext()).split())
            if txt:
                sections.append(txt)
        else:
            for child in node:
                traverse(child)
    traverse(article_body)
    size = sum(len(s.encode()) for s in sections)
    if size < 1800 or size > 200_000:
        raise Retrieval("Full text needs manual review for length or completeness")
    return {f"P{i}": value for i, value in enumerate(sections, 1)}


def issns(text):
    return re.findall(r"\b[0-9]{4}-?[0-9]{3}[0-9X]\b", (text or "").upper())


SJR_YEAR = "2025"
SJR_EXPORT = "https://www.scimagojr.com/journalrank.php?type=j&year=2025&out=xls"
# A fixed copy of the 2025 SCImago export for hosts unable to retrieve the official download.
SJR_ARCHIVE = ("https://raw.githubusercontent.com/P-M-Ferreira/ranking/"
               "532ddee09d9f780a5c29c9f99f38e886231418b1/scimagojr%202025.csv")
SJR_BLOB = "6a4e1ea4868698dedf7aabb11c8bb87bedc65ac0"


def sjr_index(data):
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError:
        text = data.decode("latin-1")
    rows = csv.DictReader(io.StringIO(text), delimiter=";")
    columns = {"Type", "Issn", "SJR Best Quartile", f"Total Docs. ({SJR_YEAR})"}
    if not columns.issubset(rows.fieldnames or []):
        raise Retrieval("SCImago export unavailable or wrong reporting year")
    mapping = {}
    for row in rows:
        if (row.get("Type") or "").strip().casefold() == "journal":
            quartile = (row["SJR Best Quartile"] or "").strip()
            for value in issns(row["Issn"]):
                mapping[value.replace("-", "")] = (
                    quartile if quartile in {"Q1", "Q2", "Q3", "Q4"} else "Unreported", SJR_YEAR)
    if not mapping:
        raise Retrieval("SCImago export has no journal identifiers")
    return mapping


def journals():
    local = Path("review/scimago-2025.csv")
    try:
        data = local.read_bytes() if local.exists() else fetch(SJR_EXPORT, limit=30_000_000, timeout=7)
        mapping = sjr_index(data)
        source = "SCImago 2025 export"
    except (Retrieval, csv.Error):
        try:
            data = fetch(SJR_ARCHIVE, limit=30_000_000, timeout=20)
            header = f"blob {len(data)}\0".encode("ascii")
            if hashlib.sha1(header + data).hexdigest() != SJR_BLOB:
                raise Retrieval("Archived SCImago export changed")
            mapping = sjr_index(data)
            source = "archived SCImago 2025 export"
        except (Retrieval, csv.Error):
            mapping = {}
            source = "SCImago 2025 unavailable; unmatched outlets remain pending"
    mapping.update(manual_journals())
    return mapping, source


def manual_journals():
    mapping = {}
    with Path("review/journals.csv").open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if not (row.get("vintage") or "").strip() or not (row.get("source_url") or "").strip().startswith("https://"):
                raise ValueError("Every manual journal check needs a vintage and a source URL.")
            if row["quartile"] not in {"Q1", "Q2", "Q3", "Q4", "Unreported"}:
                raise ValueError("Invalid quartile in review/journals.csv")
            for value in issns(row["issn"]):
                mapping[value.replace("-", "")] = (row["quartile"], row["vintage"])
    return mapping
