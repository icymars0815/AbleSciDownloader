import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from paste_parser import parse_references
from import_files import import_file
from credential_vault import CredentialVault


class SmartPasteTests(unittest.TestCase):
    def test_doi_url_and_title_no_fixed_separator(self):
        r = parse_references("A fictional test paper\nhttps://doi.org/10.1234/test")[0]
        self.assertEqual("10.1234/test", r["doi"])
        self.assertEqual("A fictional test paper", r["title"])

    def test_labeled_chinese_authors(self):
        r = parse_references("标题：A fictional test paper\n作者：Alice Smith; Bob Jones\nDOI:10.1234/test")[0]
        self.assertEqual(["Alice Smith", "Bob Jones"], r["authors"])

    def test_ris_multiple_entries(self):
        r = parse_references("TY  - JOUR\nTI  - First title\nAU  - Smith, Alice\nDO  - 10.1234/one\nER  -\nTY  - JOUR\nTI  - Second title\nDO  - 10.1234/two\nER  -")
        self.assertEqual(2, len(r))
        self.assertEqual(["Smith, Alice"], r[0]["authors"])

    def test_bibtex_nested_braces(self):
        r = parse_references('@article{a, title={A {nested} title}, author={Alice Smith and Bob Jones}, doi={10.1234/test}}')[0]
        self.assertEqual("A nested title", r["title"])
        self.assertEqual(2, len(r["authors"]))

    def test_apa_citation(self):
        r = parse_references("Smith, A. & Jones, B. (2023). Example article title. A journal, 10, 22. https://doi.org/10.1234/test")[0]
        self.assertEqual("Example article title", r["title"])
        self.assertEqual(2, len(r["authors"]))

    def test_title_only_not_discarded(self):
        self.assertEqual("A title without DOI", parse_references("A title without DOI")[0]["title"])

    def test_doi_list_deduplicates(self):
        self.assertEqual(2, len(parse_references("10.1234/one\n10.1234/two\n10.1234/one")))

    def test_csv_chinese_headers_and_quoted_comma(self):
        binary = '标题,作者,DOI\n"A title, with comma",Alice Smith;Bob Jones,10.1234/test'.encode('utf-8-sig')
        r = import_file("references.csv", binary)[0]
        self.assertEqual("A title, with comma", r["title"])
        self.assertEqual(2, len(r["authors"]))

    def test_csv_gb18030(self):
        self.assertEqual("测试标题", import_file("list.csv", "标题,doi\n测试标题,10.1234/test".encode("gb18030"))[0]["title"])

    def test_docx_extracts_only_local_text(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>标题：A fictional test paper</w:t></w:r></w:p><w:p><w:r><w:t>DOI: 10.1234/test</w:t></w:r></w:p></w:body></w:document>')
        self.assertEqual("10.1234/test", import_file("list.docx", stream.getvalue())[0]["doi"])

    def test_invalid_doc_has_clear_error(self):
        with self.assertRaises(ValueError): import_file("list.doc", b"not an OLE document")

    def test_real_legacy_doc_fixture(self):
        path = Path(__file__).parent / "fixtures" / "fictional-references.doc"
        record = import_file(path.name, path.read_bytes())[0]
        self.assertEqual("10.1234/doc-fixture", record["doi"])
        self.assertEqual(["Alice Smith", "Bob Jones"], record["authors"])

    def test_multiple_multiline_citations(self):
        records = parse_references("First test title\nAlice Smith; Bob Jones\n10.1234/one\nSecond test title\n10.1234/two")
        self.assertEqual(2, len(records))
        self.assertEqual(2, len(records[0]["authors"]))

    def test_large_file_rejected(self):
        with self.assertRaises(ValueError): import_file("list.csv", b"x" * 10_000_001)

    def test_no_network_for_parsing(self):
        with patch("socket.socket", side_effect=AssertionError("offline")):
            self.assertEqual("10.1234/test", parse_references("10.1234/test")[0]["doi"])


class CredentialTests(unittest.TestCase):
    def test_opt_in_vault_roundtrip_and_no_plaintext(self):
        with tempfile.TemporaryDirectory() as folder:
            vault = CredentialVault(folder)
            self.assertFalse(vault.exists())
            vault.save("fixture@example.invalid", "fictional-password")
            self.assertNotIn(b"fictional-password", vault.file.read_bytes())
            self.assertNotIn(b"fixture@example.invalid", vault.file.read_bytes())
            self.assertEqual("fictional-password", vault.load()["password"])
            vault.forget()
            self.assertFalse(vault.exists())
