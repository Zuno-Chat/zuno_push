from twisted.trial import unittest

from zuno_push.records import ReadPoint, has_sound


class ReadPointTests(unittest.TestCase):
    def test_unread_needs_both_receipts_behind(self) -> None:
        point = ReadPoint(unthreaded_so=10, unthreaded_ts=5, threads={"$t": 20, "main": 12})
        self.assertTrue(point.is_read(10, "main"))
        self.assertTrue(point.is_read(12, "main"))
        self.assertFalse(point.is_read(13, "main"))
        self.assertTrue(point.is_read(15, "$t"))
        self.assertFalse(point.is_read(21, "$t"))
        self.assertFalse(ReadPoint().is_read(1, "main"))


class SoundTests(unittest.TestCase):
    def test_compressed_default_actions(self) -> None:
        self.assertFalse(has_sound("", False))
        self.assertTrue(has_sound("", True))

    def test_stored_actions(self) -> None:
        self.assertTrue(has_sound('["notify",{"set_tweak":"sound","value":"default"}]', False))
        self.assertFalse(has_sound('["notify"]', False))
