"""Setup 1.3.0's update finishes on the machine's own Python (3.9 and up), importing the new version's installer and
the parts of subsync it uses: no file of either may use what only Python 3.12 accepts. An f-string's expression holding
a backslash, or the f-string's own quote (PEP 701), is a SyntaxError before 3.12, so the update ended in a traceback
there instead of its message."""
import ast
import io
import os
import tokenize
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FOLDERS = ("installer", os.path.join("subsync", "subsync"))


def python_files():
    for folder in FOLDERS:
        for path, dirs, names in os.walk(os.path.join(ROOT, folder)):
            dirs[:] = [d for d in dirs if d not in ("tests", "__pycache__")]
            for name in names:
                if name.endswith(".py"):
                    yield os.path.join(path, name)


def newer_f_strings(src):
    """(line, why) of each f-string expression only Python 3.12 and later accept."""
    found = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.JoinedStr):
            continue
        whole = ast.get_source_segment(src, node) or ""
        quote = next((q for q in ('"""', "'''", '"', "'") if whole.lstrip("rRbBfFuU").startswith(q)), "")
        for part in node.values:
            if isinstance(part, ast.FormattedValue):
                text = ast.get_source_segment(src, part.value) or ""
                if "\\" in text:
                    found.append((part.value.lineno, "a backslash in an f-string's expression"))
                elif quote and len(quote) == 1 and quote in text:
                    found.append((part.value.lineno, f"the f-string's own quote ({quote}) in its expression"))
    return found


class OlderPythonTest(unittest.TestCase):
    def test_no_f_string_needs_python_3_12(self):
        bad = []
        for path in python_files():
            with open(path, encoding="utf-8") as f:
                src = f.read()
            for line, why in newer_f_strings(src):
                bad.append(f"{os.path.relpath(path, ROOT)}:{line}: {why}")
        self.assertEqual(bad, [])

    def test_the_check_finds_them(self):
        self.assertTrue(newer_f_strings('x = f"<a{f\' b=\\"{c}\\"\' if c else \'\'}>"\n'))
        self.assertEqual(newer_f_strings('x = f"{a!r} {b:>3} {c[\'d\']}"\n'), [])
