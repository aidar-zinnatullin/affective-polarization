"""Refresh discovery and optional AI coding, or build a public Shiny snapshot offline."""
import argparse
from collections import Counter
from evidence import build as build_evidence
import csv
from datetime import date
import json
import os
from pathlib import Path
import time
from agents import (Budget, DESIGNS, DRIVERS, MODEL, OUTCOMES, PROTOCOL, Pause, digest,
                    read_paper, save_json, screen, summary, utcdate)
from catalogue import (CAUSE_TERMS, SEARCHES, START_DATE, Retrieval, abstract, article,
                       full_text, journals, manual_journals, norm, search)

STATE = Path("review/state.json")
APP = Path("app")
STUDY_FIELDS = ["id","date","title","authors","doi","journal","issn","queries","status","inclusion_basis",
                "first_seen","driver","outcome","design","countries","quartile","sjr_vintage",
                "population","sample_size","instruments","hypothesis","factor","finding","locators","note"]
DETAILS = ["population","sample_size","instruments","hypothesis","factor","finding"]
FIELDS = ["driver","outcome","design","countries"]
DECISIONS = Path("review/decisions.csv")


def table(file):
    with Path(file).open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def export(file, fields, rows):
    with (APP/file).open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def stamp():
    return utcdate().isoformat()


def title_key(m):
    if not m["title"] or not m["authors"]:
        return ""
    return "|".join([norm(m["title"]), norm(m["authors"][0]), m["date"][:4]])


def discover(state):
    articles = state["articles"]
    doi_index, title_index, aliases = {}, {}, {}
    for k, r in articles.items():
        m = r["meta"]
        if m["doi"]:
            doi_index[m["doi"]] = k
        if title_key(m):
            title_index[title_key(m)] = k
        for alias in r.get("aliases", [k]):
            aliases[alias] = k
    abstracts = {}
    run = {"date": stamp(), "queries": {}}
    for label in SEARCHES:
        hits = 0
        try:
            for page in search(label):
                for work in page:
                    m = article(work)
                    hits += 1
                    key = aliases.get(m["id"]) or doi_index.get(m["doi"])
                    title_match = title_index.get(title_key(m))
                    if not key and title_match:
                        old_doi = articles[title_match]["meta"]["doi"]
                        if not old_doi or not m["doi"] or old_doi == m["doi"]:
                            key = title_match
                    key = key or m["id"]
                    r = articles.setdefault(key, {"first_seen": stamp(), "queries": [], "aliases": []})
                    if title_match and title_match != key and m["doi"] != articles[title_match]["meta"]["doi"]:
                        r["possible_duplicate"] = title_match
                        articles[title_match]["possible_duplicate"] = key
                    # Prefer the journal version, then a version with accessible full text.
                    old = r.get("meta")
                    if not old or m["id"] == old["id"] or (
                            (m["source_type"] == "journal", m["xml"]) >
                            (old["source_type"] == "journal", old["xml"])):
                        if old and set(old["issn"]) != set(m["issn"]):
                            r.pop("sjr", None)
                        if old and (old["id"], old["doi"], old["title"]) != (m["id"], m["doi"], m["title"]):
                            for field in ("screen", "screen_hash", "readings", "adjudication", "document_hash", "retrieval"):
                                r.pop(field, None)
                        r["meta"] = m
                        abstracts[key] = abstract(work)
                    if label not in r["queries"]:
                        r["queries"].append(label)
                    if m["id"] not in r["aliases"]:
                        r["aliases"].append(m["id"])
                    aliases[m["id"]] = key
                    if m["doi"]:
                        doi_index[m["doi"]] = key
                    if title_key(m):
                        title_index[title_key(m)] = key
            status = "Complete"
        except Retrieval as e:
            status = "Partial: " + str(e)
        run["queries"][label] = {"hits": hits, "status": status, "source": "OpenAlex",
                                 "query_text": SEARCHES[label] + " AND " + CAUSE_TERMS,
                                 "from_date": START_DATE, "through_date": run["date"]}
    state["runs"].append(run)
    return abstracts, run


def metadata_status(m):
    if m["retracted"]:
        return "Retracted"
    if m["language"] and m["language"] != "en":
        return "Language exclusion"
    if m["type"] in {"preprint","dissertation","book-chapter","book","editorial","review","paratext"}:
        return "Ineligible document type"
    return ""


def supported_codes(reading):
    if (reading["eligibility"] != "include" or not reading["supported"]
            or reading["driver"] not in DRIVERS or reading["design"] not in DESIGNS
            or reading["outcome"] not in OUTCOMES or not reading.get("locators")
            or not reading.get("finding", "").strip()):
        return None
    countries = tuple(sorted({norm(c) for c in reading["countries"] if c.strip()}))
    return reading["driver"], reading["outcome"], reading["design"], countries


def review_votes(record):
    votes = list(record.get("readings", []))
    if record.get("adjudication"):
        votes.append(record["adjudication"])
    return votes


def agreed_reading(record):
    """Return a cited extraction whose full set of codes has two independent votes."""
    seen = {}
    for reading in review_votes(record):
        codes = supported_codes(reading)
        if codes is not None:
            if codes in seen:
                return seen[codes]
            seen[codes] = reading
    return None


def needs_tiebreak(record):
    pair = record.get("readings", [])
    if len(pair) != 2 or record.get("adjudication") or agreed_reading(record):
        return False
    exclusions = sum(r["eligibility"] == "exclude" for r in pair)
    # A third vote must be able to corroborate an inclusion or an exclusion.
    return exclusions == 1 or (exclusions == 0 and any(supported_codes(r) for r in pair))


def in_update_window(value):
    try:
        published = date.fromisoformat(value)
    except (TypeError, ValueError):
        return False
    return date.fromisoformat(START_DATE) <= published <= utcdate()


def code_article(budget, r, abs_text):
    m = r["meta"]
    if metadata_status(m) or r.get("possible_duplicate"):
        return
    if r.get("protocol") != PROTOCOL or r.get("model") != MODEL:
        for k in ("screen", "readings", "adjudication", "document_hash", "retrieval", "screen_hash"):
            r.pop(k, None)
        r["protocol"] = PROTOCOL
        r["model"] = MODEL
    if abs_text:
        signature = digest([m["title"], abs_text])
        if signature != r.get("screen_hash"):
            decision = screen(budget, m["title"], abs_text)
            if r.get("screen_hash"):
                for k in ("readings", "adjudication", "document_hash"):
                    r.pop(k, None)
            r["screen"] = decision
            r["screen_hash"] = signature
    elif "screen" not in r:
        r["screen"] = {"decision": "uncertain", "reason": "Abstract unavailable. Full text required."}
    if r.get("screen", {}).get("decision") == "exclude" or not m["xml"]:
        return
    if len(r.get("readings", [])) == 2 and not needs_tiebreak(r):
        return
    try:
        paragraphs = full_text(m)
    except (Retrieval, OSError) as e:
        r["retrieval"] = str(e)
        return
    r.pop("retrieval", None)
    doc_hash = digest(paragraphs)
    if r.get("document_hash") != doc_hash:
        r["document_hash"] = doc_hash
        r["readings"] = []
        r.pop("adjudication", None)
    while len(r["readings"]) < 2:
        r["readings"].append(read_paper(budget, m["title"], paragraphs, bool(r["readings"])))
    if needs_tiebreak(r):
        r["adjudication"] = read_paper(budget, m["title"], paragraphs, adjudicate=True)


def approvals(state):
    result = {}
    for row in table(DECISIONS):
        key = row["id"].strip()
        if key in result or key not in state["articles"] or row["decision"] not in {"include", "exclude"}:
            raise ValueError("Review decisions need a unique existing ID and include or exclude.")
        if any(not (row.get(field) or "").strip() for field in ["reviewer","checked_on","note"]):
            raise ValueError(f"Missing reviewer, date, or note for {key}.")
        result[key] = row
    return result


def classify(r):
    m = r["meta"]
    readings = r.get("readings", [])
    status = metadata_status(m)
    if status:
        return status
    if r.get("possible_duplicate"):
        return "Check possible duplicate"
    if (r.get("protocol") not in {None, PROTOCOL}
            or (readings and (r.get("protocol") != PROTOCOL or r.get("model") != MODEL))):
        return "Awaiting updated codebook"
    if r.get("screen", {}).get("decision") == "exclude":
        return "AI abstract exclusion"
    if not m["xml"] and len(readings) < 2 and r.get("screen"):
        return "Awaiting full text"
    if len(readings) < 2:
        if r.get("retrieval"):
            return "Check full text"
        if len(readings) == 1:
            return "Awaiting second reading"
        return "Awaiting full-text coding" if r.get("screen") else "Discovered"
    if sum(v["eligibility"] == "exclude" for v in review_votes(r)) >= 2:
        return "AI full-text exclusion"
    if not agreed_reading(r):
        return "Needs adjudication"
    if not in_update_window(m["date"]):
        return "Check publication date"
    if m["language"] != "en" or m["source_type"] != "journal" or m["type"] not in {"article", "journal-article"}:
        return "Check journal article metadata"
    if not r.get("sjr"):
        return "AI proposal: check outlet"
    return "AI-reviewed inclusion"


def study_rows(state):
    decisions = approvals(state)
    checks = manual_journals()
    rows = []
    for k, stored in state["articles"].items():
        r = dict(stored)
        m = r["meta"]
        for issn in m["issn"]:
            match = checks.get(issn.replace("-", "").upper())
            if match:
                r["sjr"] = match
                break
        status = classify(r)
        code = agreed_reading(r) or (r.get("readings") or [{}])[0]
        quartile, vintage = r.get("sjr") or ("", "")
        row = {**{k2: m[k2] for k2 in ["date","title","doi","journal"]}, "authors": "; ".join(m["authors"]),
               "issn": "; ".join(m["issn"]), "id": k, "first_seen": r["first_seen"],
               "queries": " | ".join(r["queries"]), "status": status,
               "note": r.get("retrieval") or code.get("reason") or r.get("screen", {}).get("reason", ""),
               "quartile": quartile, "sjr_vintage": vintage,
               "countries": " | ".join(dict.fromkeys(code.get("countries", [])))}
        for field in ["driver","outcome","design","locators"] + DETAILS:
            row[field] = code.get(field, "")
        decision = decisions.get(k)
        if decision and not m["retracted"]:
            row["note"] = decision["note"]
            if decision["decision"] == "exclude":
                row["status"] = "Verified exclusion"
            else:
                for field in FIELDS + DETAILS + ["locators"]:
                    value = (decision.get(field) or "").strip()
                    if value:
                        row[field] = value
                if not (in_update_window(m["date"]) and r.get("sjr") and row["driver"] in DRIVERS and row["design"] in DESIGNS
                        and row["outcome"] in OUTCOMES and row["finding"] and row["locators"]):
                    row["status"] = "Review incomplete"
                else:
                    row["status"] = "Verified inclusion"
        basis = {"AI-reviewed inclusion": "AI", "Verified inclusion": "Reviewer"}
        row["inclusion_basis"] = basis.get(row["status"], "") if "Primary" in r["queries"] else ""
        rows.append(row)
    return sorted(rows, key=lambda row: (row["date"], row["id"]), reverse=True)


def evidence_by_family(rows, driver):
    return [{"id": r["id"], "design": r["design"], "outcome": r["outcome"], "finding": r["finding"],
             "basis": r["inclusion_basis"]}
            for r in rows if r["inclusion_basis"] and r["driver"] == driver]


def synthesize(state, budget, rows):
    for note in table(APP/"baseline_notes.csv"):
        driver = note["driver"]
        evidence = evidence_by_family(rows, driver)
        fingerprint = digest([PROTOCOL, evidence])
        if evidence and state["briefs"].get(driver, {}).get("signature") != fingerprint:
            draft = summary(budget, driver, note["note"], evidence)
            state["briefs"][driver] = {"signature": fingerprint, "text": draft, "date": stamp()}


def render(state):
    rows = study_rows(state)
    export("studies.csv", STUDY_FIELDS, rows)
    history = [{"date": r["date"], "query": label, **item}
               for r in state["runs"] for label, item in r["queries"].items()]
    export("coverage.csv", ["date","query","hits","status"], history)
    notes = []
    for n in table(APP/"baseline_notes.csv"):
        family = n["driver"]
        draft = state["briefs"].get(family, {})
        evidence = evidence_by_family(rows, family)
        if evidence and draft.get("signature") == digest([PROTOCOL, evidence]):
            notes.append({"driver": family, "draft": draft["text"], "date": draft["date"]})
    export("briefs.csv", ["driver","draft","date"], notes)
    status = state.get("last_work", "Monthly update has not run.")
    export("run.csv", ["message","model","protocol"], [{"message": status,"model": MODEL,"protocol": PROTOCOL}])
    pairs = [r["readings"] for r in state["articles"].values()
             if r.get("protocol") == PROTOCOL and len(r.get("readings", [])) == 2
             and all(x["eligibility"] == "include" and x["driver"] in DRIVERS for x in r["readings"])]
    mismatches = sum(a["driver"] != b["driver"] for a, b in pairs)
    labels = Counter(reading["driver"] for pair in pairs for reading in pair)
    n = 2 * len(pairs)
    expected = (n*n - sum(v*v for v in labels.values())) / (n*(n-1)) if n >= 2 else 0
    alpha = round(1 - (mismatches / len(pairs))/expected, 3) if expected else ""
    export("agreement.csv", ["pairs","agreements","alpha"], [{
        "pairs": len(pairs), "agreements": len(pairs)-mismatches, "alpha": alpha}])


def refresh(state):
    if not os.getenv("OPENALEX_API_KEY"):
        state["last_work"] = "Discovery is waiting for an OpenAlex key."
        return
    abstracts, run = discover(state)
    catalogue, journal_source = journals()
    for r in state["articles"].values():
        for issn in r["meta"]["issn"]:
            match = catalogue.get(issn.replace("-", "").upper())
            if match:
                r["sjr"] = list(match)
                break
    if not os.getenv("OPENAI_API_KEY"):
        state["last_work"] = f"{stamp()} · Discovery ran. AI screening is off. Journal check: {journal_source}."
        return
    budget = Budget()
    stop = time.monotonic() + 270
    message = "Queue processed."
    try:
        synthesize(state, budget, study_rows(state))
        reviewed = approvals(state)
        queue = sorted(state["articles"].items(), key=lambda x: (
            "Primary" not in x[1]["queries"], len(x[1].get("readings", [])) != 1,
            x[1]["first_seen"], x[1]["meta"]["date"], x[0]))
        for k, r in queue:
            if k in reviewed:
                continue
            if time.monotonic() >= stop:
                message = "Run time reached. Remaining records are queued."
                break
            code_article(budget, r, abstracts.get(k, ""))
        if time.monotonic() < stop:
            synthesize(state, budget, study_rows(state))
    except Pause as e:
        message = str(e)
    if any(v["status"] != "Complete" for v in run["queries"].values()):
        message += " See search coverage for incomplete queries."
    state["last_work"] = f"{stamp()} · {message} Journal check: {journal_source}."


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["refresh","snapshot"])
    mode = parser.parse_args().mode
    state = json.loads(STATE.read_text())
    if mode == "refresh":
        refresh(state)
        save_json(STATE, state)
    render(state)
    (APP/"evidence.html").write_text(build_evidence(APP), encoding="utf-8")
    print("Snapshot ready.", len(state["articles"]), "canonical records in the update register.")
