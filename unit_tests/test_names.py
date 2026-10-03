import json
import os

from twisted.trial import unittest

from unit_tests.fakes import FakeEvent
from zuno_push.names import (
    blob_name,
    cut_utf8,
    ignores,
    is_direct,
    member_name,
    room_title,
    strip_controls,
)

NAMES_FIXTURE = os.path.join(os.path.dirname(__file__), "..", "fixtures", "push", "names_v1.json")


def ev(content: dict) -> FakeEvent:
    return FakeEvent(type="x", sender="@s:test", content=content)


def name_cases(document: object) -> list:
    candidates = [document] if isinstance(document, list) else []
    if isinstance(document, dict):
        candidates = [document.get("cases"), *document.values()]
    for candidate in candidates:
        if (
            isinstance(candidate, list)
            and candidate
            and all(isinstance(c, dict) and "input" in c and "output" in c for c in candidate)
        ):
            return candidate
    raise AssertionError("names_v1.json holds no list of {input, output} cases")


class NameTests(unittest.TestCase):
    def test_cut_keeps_the_longest_prefix_of_whole_scalars(self) -> None:
        self.assertEqual(cut_utf8("a" * 70), "a" * 64)
        cut = cut_utf8("é" * 40)
        self.assertEqual(len(cut.encode("utf-8")), 64)
        self.assertEqual(cut_utf8("a" * 63 + "😀"), "a" * 63)
        self.assertEqual(cut_utf8("a\ud800b"), "ab")

    def test_controls_and_bidi_marks_are_stripped(self) -> None:
        self.assertEqual(strip_controls("A\x00l\x1fi\x7fc\x85e\x9f"), "Alice")
        self.assertEqual(strip_controls("‮evil‬"), "evil")
        self.assertEqual(strip_controls("‎a‏‪b‭c⁦d⁩"), "abcd")
        self.assertEqual(strip_controls("Ana؜is"), "Anais")
        self.assertEqual(strip_controls("👩‍💻 Zoë"), "👩‍💻 Zoë")
        self.assertEqual(strip_controls("⁥⁪ "), "⁥⁪ ")

    def test_strip_happens_before_the_cut(self) -> None:
        self.assertEqual(blob_name("‮" * 30 + "a" * 70), "a" * 64)
        self.assertEqual(blob_name("\x00" * 64), "")

    def test_member_and_room_names(self) -> None:
        self.assertEqual(member_name(ev({"displayname": "Alice"})), "Alice")
        self.assertEqual(member_name(ev({"displayname": "‮Alice"})), "Alice")
        self.assertEqual(member_name(ev({"displayname": 5})), "")
        self.assertEqual(member_name(None), "")
        self.assertEqual(room_title(ev({"name": "Design"}), ev({"alias": "#d:hs"})), "Design")
        self.assertEqual(room_title(None, ev({"alias": "#d:hs"})), "#d:hs")
        self.assertEqual(room_title(ev({"name": "⁦⁩"}), ev({"alias": "#d:hs"})), "#d:hs")
        self.assertEqual(room_title(ev({"name": ""}), None), "")

    def test_direct_and_ignore_lists(self) -> None:
        self.assertTrue(is_direct({"@b:hs": ["!r:hs"]}, "!r:hs"))
        self.assertFalse(is_direct({"@b:hs": "!r:hs"}, "!r:hs"))
        self.assertFalse(is_direct(None, "!r:hs"))
        self.assertTrue(ignores({"ignored_users": {"@b:hs": {}}}, "@b:hs"))
        self.assertFalse(ignores({"ignored_users": ["@b:hs"]}, "@b:hs"))
        self.assertFalse(ignores(None, "@b:hs"))


class SharedNameVectorTests(unittest.TestCase):
    def test_every_names_case_from_the_app_repo(self) -> None:
        if not os.path.exists(NAMES_FIXTURE):
            raise unittest.SkipTest("copy the app repo's test/fixtures/push/ into fixtures/push/")
        with open(NAMES_FIXTURE, encoding="utf-8") as f:
            cases = name_cases(json.load(f))
        for case in cases:
            self.assertEqual(blob_name(case["input"]), case["output"], case)
