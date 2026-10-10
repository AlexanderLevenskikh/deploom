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

    def test_binary_and_unknown_encoding_have_no_text_identity(self):
        for data in (b"first\0\r\n", b"\xff\r\n", "first\r\n".encode("utf-16"), b"a\x01b"):
            self.assertIsNone(text_identity(data))
