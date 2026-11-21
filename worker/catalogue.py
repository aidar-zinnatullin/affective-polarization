"""Bibliographic search, journal lookup, and open full-text retrieval."""
import csv
from difflib import SequenceMatcher
import gzip
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


def journals():
    """Use a dated official export when available. Manual ISSN checks override it."""
    snapshot_year = 2025
    mapping = {}
    origin = f"https://www.scimagojr.com/journalrank.php?type=j&year={snapshot_year}&out=xls"
    local = Path("review/scimago-2025.csv")
    try:
        data = local.read_bytes() if local.exists() else fetch(origin, limit=30_000_000, timeout=7)
        try:
            txt = data.decode("utf-8-sig")
        except UnicodeError:
            txt = data.decode("latin-1")
        rows = csv.DictReader(io.StringIO(txt), delimiter=";")
        if "SJR Best Quartile" not in (rows.fieldnames or []):
            raise Retrieval("SCImago export unavailable")
        for row in rows:
            if (row.get("Type") or "").casefold() != "journal":
                continue
            for value in issns(row.get("Issn")):
                q = row["SJR Best Quartile"]
                mapping[value.replace("-", "")] = (q if q in {"Q1","Q2","Q3","Q4"} else "Unreported",
                                                    str(snapshot_year))
    except (Retrieval, csv.Error):
        pass
    mapping.update(manual_journals())
    return mapping


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
