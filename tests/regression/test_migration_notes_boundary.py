"""Only owned native comments, never literals/data, may be cleaned."""
import shutil
import unittest
from pathlib import Path

from iterative_delivery_notes import without_migration_notes, assert_comment_only_removal


class MigrationNotesBoundaryTests(unittest.TestCase):
    def test_owned_full_line_only_preserving_bom_crlf_and_other_comments(self):
        data = (b"\xef\xbb\xbf// DEPLOOM-MIGRATION-NOTE:run-a: why\r\n"
                b"// ordinary\r\n// DEPLOOM-MIGRATION-NOTE:run-b: other run\r\n"
                b"const value = 1; // DEPLOOM-MIGRATION-NOTE:run-a: inline\r\n")
        after, count = without_migration_notes(data, "source.ts", "run-a")
        self.assertEqual(count, 1)
        self.assertEqual(after, b"\xef\xbb\xbf" + data.split(b"\r\n", 1)[1])

    def test_json_data_and_unknown_file_formats_are_preserved(self):
        data = b'// DEPLOOM-MIGRATION-NOTE:run-a: is not a JSON comment\n'
        self.assertEqual(without_migration_notes(data, "package.json", "run-a"), (data, 0))

    def test_python_literal_marker_is_rejected_but_native_comment_is_allowed(self):
        good = b"# DEPLOOM-MIGRATION-NOTE:run-a: why\nx = 1\n"
        assert_comment_only_removal(good, without_migration_notes(good, "input.py", "run-a")[0], "input.py", {}, {})
        literal = b'value = """\n# DEPLOOM-MIGRATION-NOTE:run-a: literal data\n"""\n'
        with self.assertRaisesRegex(RuntimeError, "MARKER_IN_CODE_OR_DATA"):
            assert_comment_only_removal(literal, without_migration_notes(literal, "input.py", "run-a")[0], "input.py", {}, {})

    @unittest.skipUnless(shutil.which("node"), "Node required")
    def test_toml_and_yaml_comments_are_allowed_but_block_literals_are_data(self):
        state = {"workspaceRoot": str(Path(__file__).resolve().parents[2] / "desktop")}
        for name, good, literal in (
            ("input.toml", b"# DEPLOOM-MIGRATION-NOTE:run-a: why\nvalue = 1\n", b'value = """\n# DEPLOOM-MIGRATION-NOTE:run-a: data\n"""\n'),
            ("input.yaml", b"# DEPLOOM-MIGRATION-NOTE:run-a: why\nvalue: 1\n", b"value: |-\n  # DEPLOOM-MIGRATION-NOTE:run-a: data\n"),
        ):
            assert_comment_only_removal(good, without_migration_notes(good, name, "run-a")[0], name, {}, state)
            with self.assertRaisesRegex(RuntimeError, "MARKER_IN_CODE_OR_DATA"):
                assert_comment_only_removal(literal, without_migration_notes(literal, name, "run-a")[0], name, {}, state)

    @unittest.skipUnless(shutil.which("node"), "Node required")
    def test_real_typescript_ast_rejects_template_literal_marker(self):
        root = str(Path(__file__).resolve().parents[2] / "desktop")
        state = {"workspaceRoot": root}
        good = b"// DEPLOOM-MIGRATION-NOTE:run-a: why\nexport const value = 1;\n"
        assert_comment_only_removal(good, without_migration_notes(good, "input.ts", "run-a")[0], "input.ts", {}, state)
        literal = b"export const value = `\n// DEPLOOM-MIGRATION-NOTE:run-a: literal data\n`;\n"
        with self.assertRaisesRegex(RuntimeError, "MARKER_IN_CODE_OR_DATA"):
            assert_comment_only_removal(literal, without_migration_notes(literal, "input.ts", "run-a")[0], "input.ts", {}, state)

    @unittest.skipUnless(shutil.which("node"), "Node required")
    def test_real_css_ast_accepts_comment_but_rejects_string_data_marker(self):
        root = str(Path(__file__).resolve().parents[2] / "desktop")
        state = {"workspaceRoot": root}
        good = b"/* DEPLOOM-MIGRATION-NOTE:run-a: why */\n.a { color: red; }\n"
        assert_comment_only_removal(good, without_migration_notes(good, "input.css", "run-a")[0], "input.css", {}, state)
        literal = b'.a { content: "line\\\n/* DEPLOOM-MIGRATION-NOTE:run-a: data */\\\nend"; }\n'
        self.assertEqual(without_migration_notes(literal, "input.css", "run-a"), (literal, 0))
        lines = literal.splitlines(keepends=True)
        changed = lines[0] + lines[2]
        with self.assertRaisesRegex(RuntimeError, "MARKER_IN_CODE_OR_DATA"):
            assert_comment_only_removal(literal, changed, "input.css", {}, state)


if __name__ == "__main__":
    unittest.main()
