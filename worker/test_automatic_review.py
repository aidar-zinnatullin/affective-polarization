"""Exercise the automatic review and chart update without contacting either API."""
import csv
import hashlib
import os
import re
import shutil
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import agents
import catalogue
import evidence
import update


ROOT = Path(__file__).resolve().parents[1]


class AutomaticReviewTest(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(work.name)
        Path("app").mkdir()
        Path("review").mkdir()
        for name in ("baseline.csv", "annual.csv", "baseline_notes.csv"):
            shutil.copyfile(ROOT / "app" / name, Path("app") / name)
        for name in ("decisions.csv", "journals.csv"):
            src = ROOT / "review" / name
            (Path("review") / name).write_text(src.read_text().splitlines()[0] + "\n")
        self.first = {
            "eligibility": "include", "driver": agents.DRIVERS[0], "outcome": "Horizontal",
            "design": "Panel", "countries": ["Spain"], "finding": "A cited association.",
            "supported": True, "locators": "driver P1, outcome P2, design P3, finding P4",
        }
        self.record = {
            "meta": {"id": "W1", "date": "2025-10-02", "doi": "10.1234/test", "journal": "Test",
                     "issn": ["1234-5678"], "title": "Test", "authors": [], "language": "en",
                     "retracted": False, "type": "article", "source_type": "journal", "xml": True},
            "queries": ["Primary", "Partisan animosity"], "first_seen": "2025-10-03",
            "sjr": ["Q1", "2025"], "protocol": agents.PROTOCOL, "model": agents.MODEL,
            "screen": {"decision": "advance"}, "readings": [deepcopy(self.first), deepcopy(self.first)],
        }
        self.state = {"articles": {"W1": self.record}, "runs": [], "briefs": {}}

    def combined(self):
        update.render(self.state)
        page = evidence.build(Path("app"))
        self.assertIn('id="ap-corpus-0" checked><label for="ap-corpus-0">Published + AI-reviewed', page)
        self.assertIn('id="ap-form0-0" checked><label for="ap-form0-0">All forms', page)
        return re.search(r'<p class="ap-total">All forms · (\d+) articles</p>', page)[1]

    def decision(self, value):
        path = Path("review/decisions.csv")
        with path.open() as f:
            header = next(csv.reader(f))
        with path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=header)
            writer.writeheader()
            writer.writerow(dict(id="W1", decision=value, reviewer="QA", checked_on="2025-11-01",
                                 note="Checked primary report", driver=agents.DRIVERS[2]))

    def test_automatic_inclusion_and_overrides(self):
        self.assertEqual(self.combined(), "136")
        self.assertEqual(update.study_rows(self.state)[0]["inclusion_basis"], "AI")
        self.decision("include")
        self.assertEqual(self.combined(), "136")
        row = update.study_rows(self.state)[0]
        self.assertEqual((row["driver"], row["inclusion_basis"]), (agents.DRIVERS[2], "Reviewer"))
        self.assertEqual(len(update.evidence_by_family([row], agents.DRIVERS[2])), 1)
        self.record["meta"]["retracted"] = True
        self.assertEqual(self.combined(), "135")
        self.record["meta"]["retracted"] = False
        self.decision("exclude")
        self.assertEqual(self.combined(), "135")
        self.assertFalse(update.evidence_by_family(update.study_rows(self.state), agents.DRIVERS[0]))

    def test_no_automatic_entry_for_ineligible_or_uncertain_records(self):
        original = deepcopy(self.record)
        for field, value in (("date", "2025-08-22"), ("date", "2099-01-01"), ("date", ""),
                             ("language", "de"), ("type", "preprint"), ("source_type", "repository")):
            with self.subTest(field=field, value=value):
                self.record["meta"] = dict(original["meta"], **{field: value})
                self.assertEqual(self.combined(), "135")
        self.record["meta"] = original["meta"]
        for field, value in (("queries", ["Negative partisanship"]), ("sjr", None),
                             ("possible_duplicate", "W2"), ("readings", [self.first]),
                             ("readings", [self.first, dict(self.first, supported=False)])):
            with self.subTest(field=field):
                self.state["articles"]["W1"] = dict(self.record, **{field: value})
                self.assertEqual(self.combined(), "135")

    def test_tiebreak_selects_corroborated_reading_and_is_idempotent(self):
        paragraphs = {"P1": "Fixture."}
        second = dict(self.first, outcome="Vertical", finding="Second, supported finding.")
        self.record["readings"][1] = second
        self.record["document_hash"] = agents.digest(paragraphs)
        self.assertEqual(self.combined(), "135")
        with patch.object(update, "full_text", return_value=paragraphs), \
             patch.object(update, "read_paper", side_effect=[agents.Pause("Pause"), dict(second)]) as read:
            with self.assertRaises(agents.Pause):
                update.code_article(None, self.record, "")
            update.code_article(None, self.record, "")
            update.code_article(None, self.record, "")
            self.assertEqual(read.call_count, 2)
            self.assertTrue(read.call_args.kwargs["adjudicate"])
        self.assertEqual(len(self.record["readings"]), 2)
        row = update.study_rows(self.state)[0]
        self.assertEqual((row["outcome"], row["finding"]), (second["outcome"], second["finding"]))
        self.assertEqual(self.combined(), "136")
        self.record["adjudication"] = dict(self.first, outcome="Both")
        self.assertEqual(self.combined(), "135")
        self.record["readings"][1] = {"eligibility": "exclude"}
        self.record["adjudication"] = {"eligibility": "exclude"}
        self.assertEqual(update.classify(self.record), "AI full-text exclusion")

    def test_rankings_fallback_is_validated(self):
        sample = b"Type;Issn;SJR Best Quartile;Total Docs. (2025)\njournal;12345678;Q3;5\n"
        blob = hashlib.sha1(f"blob {len(sample)}\0".encode() + sample).hexdigest()
        with patch.object(catalogue, "fetch", side_effect=[b"<html>Unavailable</html>", sample]), \
             patch.object(catalogue, "SJR_BLOB", blob):
            ranks, source = catalogue.journals()
            self.assertEqual(ranks["12345678"], ("Q3", "2025"))
            self.assertIn("archived", source)
        with patch.object(catalogue, "fetch", side_effect=[b"<html>Unavailable</html>", sample]):
            ranks, source = catalogue.journals()
            self.assertEqual(ranks, {})
            self.assertIn("unavailable", source)

    def test_refresh_promotes_and_synthesizes_without_decision_rows(self):
        self.record.pop("sjr")
        self.record.pop("readings")
        with patch.dict(os.environ, OPENALEX_API_KEY="test", OPENAI_API_KEY="test"), \
             patch.object(update, "discover", return_value=({}, {"queries": {}})), \
             patch.object(update, "journals", return_value=({"12345678": ("Q1", "2025")}, "fixture")), \
             patch.object(update, "full_text", return_value={"P1": "Fixture."}), \
             patch.object(update, "read_paper", side_effect=[self.first, deepcopy(self.first)]) as reads, \
             patch.object(update, "summary", return_value="Cited finding [W1].") as draft:
            update.refresh(self.state)
            update.refresh(self.state)
            self.assertEqual(reads.call_count, 2)
            self.assertEqual(draft.call_count, 1)
        self.assertEqual(self.combined(), "136")
        self.assertEqual(update.table("app/briefs.csv")[0]["draft"], "Cited finding [W1].")


if __name__ == "__main__":
    unittest.main()
