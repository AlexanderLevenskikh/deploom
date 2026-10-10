"""EOL equivalence must not hide binary, encoding, BOM or whitespace changes."""
import unittest
from delivery_source_identity import text_identity


class TextIdentityTests(unittest.TestCase):
    def test_all_three_line_endings_and_mixed_forms_share_text_identity(self):
        expected = text_identity(b"first\nsecond\nthird\n")["textHash"]
        for data in (b"first\rsecond\rthird\r", b"first\r\nsecond\r\nthird\r\n",
                     b"first\rsecond\r\nthird\n"):
            self.assertEqual(text_identity(data)["textHash"], expected)

    def test_bom_whitespace_final_newline_and_content_remain_significant(self):
        original = text_identity(b"first\nsecond\n")["textHash"]
        for data in (b"\xef\xbb\xbffirst\nsecond\n", b"first \nsecond\n",
                     b"first\nsecond", b"first\nchanged\n"):
            self.assertNotEqual(text_identity(data)["textHash"], original)

    def test_cp1251_and_unicode_bom_eol_changes_preserve_all_other_bytes(self):
        for codec, bom in (("cp1251", b""), ("utf-16-le", b"\xff\xfe"), ("utf-16-be", b"\xfe\xff"),
                           ("utf-32-le", b"\xff\xfe\0\0"), ("utf-32-be", b"\0\0\xfe\xff")):
            a = bom + "SQL: обновление\r\nGO\r\n".encode(codec)
            b = bom + "SQL: обновление\nGO\n".encode(codec)
            self.assertEqual(text_identity(a)["textHash"], text_identity(b)["textHash"])
            self.assertNotEqual(text_identity(a)["textHash"], text_identity(bom + "SQL: изменение\nGO\n".encode(codec))["textHash"])
        self.assertNotEqual(text_identity("обновление\n".encode("cp1251"))["textHash"], text_identity("обновление\n".encode("utf-8"))["textHash"])

    def test_binary_or_malformed_unicode_has_no_text_identity(self):
        for data in (b"first\0\r\n", b"a\x01b", b"\xff\xfe\x01", b"\xfe\xff\xd8\x00"):
            self.assertIsNone(text_identity(data))
