import tempfile
import textwrap
import unittest
from pathlib import Path

import pymupdf

from core import verify_pdf

DOI = "10.1234/layout.fixture"
TITLE = "Synthetic layout fixture for comparison checks"
ABSTRACT = ("This fictional paragraph exists only to exercise the local abstract comparison. "
            "It describes an imaginary pipeline that reads page text in several orders, "
            "compares sliding windows of characters against a stored reference record, "
            "and reports a similarity score without representing any real measurement or published result.")
WRONG = ("Entirely different invented content about telescope mirrors, orbital mechanics and "
         "the calibration of radio antennas across several observing seasons, written so that "
         "it shares almost no vocabulary with the stored reference record for this fixture article.")
REFERENCE = dict(status="available", doi=DOI, title=TITLE, authors=["Alice Smith", "Bob Jones"],
                 abstract=ABSTRACT, sources=["OpenAlex"])
HEAD = [TITLE, "Alice Smith, Bob Jones", "doi:" + DOI]
BODY = "Introduction\n" + "Filler sentence for the body of the synthetic article. " * 12


def wrap(text, width=70):
    return textwrap.wrap(text, width)


def build(path, pages):
    """pages: list of page specs; each is a list of (x, y, line) tuples."""
    doc = pymupdf.open()
    for lines in pages:
        page = doc.new_page()
        for x, y, line in lines:
            page.insert_text((x, y), line, fontsize=9)
    doc.save(path)
    doc.close()


def flow(lines, x=50, y=60, dy=13):
    return [(x, y + i * dy, line) for i, line in enumerate(lines)]


def head_lines():
    return flow(HEAD)


class AbstractLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def check(self, pages):
        path = str(Path(self.temp.name) / "x.pdf")
        build(path, pages)
        return verify_pdf(path, TITLE, DOI, metadata=REFERENCE)["bibliography"]

    def page1(self, heading_lines, abstract=ABSTRACT):
        lines = head_lines() + flow(heading_lines + wrap(abstract) + ["Keywords: synthetic"], y=120)
        return [lines]

    def test_plain_abstract_heading(self):
        c = self.check(self.page1(["ABSTRACT"]))
        self.assertEqual("match", c["abstract_status"])

    def test_spaced_heading(self):
        c = self.check(self.page1(["A B S T R A C T"]))
        self.assertEqual("match", c["abstract_status"])
        self.assertEqual("heading", c["abstract_method"])

    def test_scope_heading_structured_abstract(self):
        lines = wrap("Scope: " + ABSTRACT)
        c = self.check([head_lines() + flow(lines + ["Keywords: synthetic"], y=120)])
        self.assertEqual("match", c["abstract_status"])

    def test_inline_abstract_label(self):
        lines = wrap("ABSTRACT: " + ABSTRACT)
        c = self.check([head_lines() + flow(["Journal of Fixtures 2020, 1, 1-9   "] + lines, y=120)])
        self.assertEqual("match", c["abstract_status"])

    def test_cover_page_then_abstract_on_page_two(self):
        cover = flow(["Just Accepted", "This is a cover page with a notice about manuscripts."] * 2)
        page2 = head_lines() + flow(["ABSTRACT: " + w for w in [""]] + wrap("ABSTRACT: " + ABSTRACT), y=120)
        c = self.check([cover, page2])
        self.assertEqual("match", c["abstract_status"])

    def test_two_column_interleave_uses_default_order(self):
        left = wrap(ABSTRACT, 50)
        right = ["Article history:", "Received 1 January 2020", "Accepted 2 February 2020",
                 "Available online 3 March 2020", "Keywords:", "synthetic", "fixture"] * 3
        lines = head_lines() + [(50, 120, "A B S T R A C T")]
        # Left column is drawn first, so default order is contiguous while sort=True interleaves by y.
        lines += [(50, 135 + i * 13, text) for i, text in enumerate(left)]
        lines += [(380, 135 + i * 13, text) for i, text in enumerate(right[:len(left)])]
        c = self.check([lines])
        self.assertEqual("match", c["abstract_status"])

    def test_heading_free_window_match(self):
        c = self.check([head_lines() + flow(wrap(ABSTRACT) + wrap(BODY), y=120)])
        self.assertEqual("match", c["abstract_status"])
        self.assertEqual("window", c["abstract_method"])

    def test_wrong_abstract_with_heading_is_difference(self):
        c = self.check(self.page1(["ABSTRACT"], WRONG))
        self.assertEqual("difference", c["abstract_status"])

    def test_wrong_abstract_without_heading_is_unverified(self):
        c = self.check([head_lines() + flow(wrap(WRONG) + wrap(BODY), y=120)])
        self.assertEqual("unverified", c["abstract_status"])
        self.assertNotEqual("difference", c["status"])

    def test_list_and_string_inputs_are_accepted(self):
        from core import compare_metadata
        text = "\n".join(HEAD) + "\nABSTRACT\n" + ABSTRACT
        a = compare_metadata(text, text, DOI, REFERENCE)
        b = compare_metadata(text, [text, text], DOI, REFERENCE)
        self.assertEqual(a["abstract_status"], b["abstract_status"])


if __name__ == "__main__":
    unittest.main()
