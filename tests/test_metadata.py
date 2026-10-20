import copy
import json
import tempfile
import unittest
from unittest.mock import patch

from core import compare_metadata
from metadata import fetch_metadata, reconstruct_abstract, crossref_record, verify_with_metadata

DOI = "10.1234/local.fixture"
TITLE = "Synthetic test article on local bibliography verification"
ABSTRACT = "This is a fictional software test abstract used to check local text comparison. No scientific measurements or published results are represented by this fixture."
REFERENCE = dict(status="available", doi=DOI, title=TITLE, authors=["Alice Smith", "Bob Jones"], abstract=ABSTRACT, sources=["OpenAlex"])
FRONT = TITLE + "\nAlice Smith, Bob Jones\n" + DOI + "\nABSTRACT\n" + ABSTRACT + "\nKeywords\nsoftware testing"


class ComparisonTests(unittest.TestCase):
    def compare(self, text=FRONT, reference=None):
        return compare_metadata(text, text, DOI, reference if reference is not None else REFERENCE)

    def test_title_authors_and_abstract_all_match(self):
        self.assertEqual("match", self.compare()["status"])

    def test_author_initials_are_supported(self):
        result = self.compare(FRONT.replace("Alice Smith, Bob Jones", "A. Smith, B. Jones"))
        self.assertEqual("match", result["author_status"])

    def test_missing_author_cannot_pass(self):
        result = self.compare(FRONT.replace("Alice Smith, Bob Jones", "Alice Smith"))
        self.assertEqual("partial", result["author_status"])
        self.assertEqual("unverified", result["status"])

    def test_names_in_abstract_or_references_are_not_authors(self):
        result = self.compare(FRONT.replace("Alice Smith, Bob Jones\n", "") + "\nReferences\nAlice Smith, Bob Jones")
        self.assertEqual("unverified", result["author_status"])

    def test_missing_api_abstract_is_not_required_when_title_and_authors_match(self):
        # Elsevier and others publish no abstract in the open record: title + full author list is enough.
        result = self.compare(reference=dict(REFERENCE, abstract=""))
        self.assertEqual("unavailable", result["abstract_status"])
        self.assertEqual("match", result["status"])

    def test_missing_api_abstract_with_unmatched_authors_stays_unverified(self):
        result = self.compare(reference=dict(REFERENCE, abstract="", authors=["Nobody Else", "Another Person"]))
        self.assertEqual("unavailable", result["abstract_status"])
        self.assertEqual("unverified", result["status"])

    def test_missing_pdf_abstract_is_not_invented_from_body(self):
        result = self.compare(FRONT.replace("ABSTRACT", "Introduction").replace(ABSTRACT, "Unrelated placeholder sentence. " * 8))
        self.assertEqual("unverified", result["abstract_status"])

    def test_different_abstract_needs_review(self):
        result = self.compare(FRONT.replace(ABSTRACT, "Completely unrelated materials about astronomy and telescope calibration. " * 3))
        self.assertEqual("difference", result["abstract_status"])
        self.assertEqual("difference", result["status"])

    def test_different_reference_title_is_flagged(self):
        self.assertEqual("difference", self.compare(reference=dict(REFERENCE, title="Unrelated battery materials"))["title_status"])

    def test_different_reference_doi_is_not_compared(self):
        result = self.compare(reference=dict(REFERENCE, doi="10.1234/other"))
        self.assertEqual("unverified", result["status"])
        self.assertEqual("unverified", result["title_status"])

    def test_api_failure_never_claims_multifield_match(self):
        self.assertEqual("unverified", self.compare(reference={"status": "unavailable"})["status"])


class MetadataFetchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def raw_openalex(self, abstract=True):
        return dict(doi="https://doi.org/" + DOI, title=TITLE,
                    authorships=[{"author": {"display_name": "Alice Smith"}}],
                    abstract_inverted_index={word: [i for i, w in enumerate(ABSTRACT.split()) if w == word] for word in ABSTRACT.split()} if abstract else None)

    def test_inverted_abstract_is_reconstructed_in_position_order(self):
        self.assertEqual("A B A", reconstruct_abstract({"B": [1], "A": [0, 2]}))

    def test_openalex_success_only_transmits_doi(self):
        with patch("metadata.get_json", return_value=self.raw_openalex()) as query:
            record = fetch_metadata(DOI, self.temp.name)
        self.assertEqual(["OpenAlex"], record["sources"])
        self.assertEqual(ABSTRACT, record["abstract"])
        self.assertEqual(2, query.call_count)   # OpenAlex, then Crossref for update notices
        self.assertIn("https://doi.org/" + DOI, query.call_args_list[0].args[0])
        self.assertNotIn("mail", query.call_args_list[0].args[0] + query.call_args_list[1].args[0])

    def test_metadata_cache_avoids_repeated_requests(self):
        with patch("metadata.get_json", return_value=self.raw_openalex()) as query:
            fetch_metadata(DOI, self.temp.name)
            cached = fetch_metadata(DOI, self.temp.name)
        self.assertEqual(2, query.call_count)
        self.assertTrue(cached["cached"])

    def test_crossref_fills_missing_abstract_with_source_recorded(self):
        crossref = {"message": {"DOI": DOI, "title": [TITLE], "abstract": "<jats:p>" + ABSTRACT + "</jats:p>"}}
        with patch("metadata.get_json", side_effect=[self.raw_openalex(False), crossref]):
            record = fetch_metadata(DOI, self.temp.name)
        self.assertEqual("Crossref", record["field_sources"]["abstract"])
        self.assertEqual("OpenAlex", record["field_sources"]["authors"])

    def test_mismatched_doi_is_rejected(self):
        wrong = self.raw_openalex()
        wrong["doi"] = "https://doi.org/10.1234/wrong"
        with patch("metadata.get_json", side_effect=[wrong, OSError("unavailable")]):
            record = fetch_metadata(DOI, self.temp.name)
        self.assertEqual("unavailable", record["status"])
        self.assertFalse(record.get("authors"))

    def test_both_apis_down_preserves_local_verification(self):
        with patch("metadata.get_json", side_effect=OSError("private proxy credentials must not be logged")):
            record = fetch_metadata(DOI, self.temp.name)
        self.assertEqual("unavailable", record["status"])
        self.assertNotIn("private proxy", json.dumps(record))

    def test_crossref_markup_is_text(self):
        self.assertEqual(" Abstract & text ", crossref_record({"message": {"abstract": "<jats:p>Abstract &amp; text</jats:p>"}})["abstract"])

    def test_no_doi_makes_no_api_request(self):
        with patch("metadata.get_json") as query:
            self.assertEqual("unavailable", fetch_metadata("", self.temp.name)["status"])
        query.assert_not_called()
