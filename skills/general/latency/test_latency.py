#!/usr/bin/env python3
"""Self-check for the decisions that are easy to get wrong."""

import io
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

namespace = {"__file__": os.path.join(HERE, "latency.py"), "__name__": "latency"}
with io.open(os.path.join(HERE, "latency.py"), encoding="utf-8") as stream:
    exec(compile(stream.read(), "latency.py", "exec"), namespace)

distribution = namespace["distribution"]
parse_rate = namespace["parse_rate"]
as_list = namespace["as_list"]
to_powershell = namespace["to_powershell"]
Failure = namespace["Failure"]


class TestDistribution(unittest.TestCase):
    def test_reports_the_tail_not_the_average(self):
        # 19 calm samples and one 800ms spike: the average looks fine, and the
        # average is exactly what must not be reported on its own.
        values = [10] * 19 + [800]
        stats = distribution(values, 100)
        self.assertEqual(stats["ok"], 20)
        self.assertEqual(stats["max"], 800)
        self.assertEqual(stats["over"], 1)
        self.assertLess(stats["avg"], 60)

    def test_losses_are_counted_not_silently_dropped(self):
        stats = distribution([10, -1, -1, 20], 100)
        self.assertEqual(stats["n"], 4)
        self.assertEqual(stats["ok"], 2)
        self.assertEqual(stats["lost"], 2)

    def test_no_answer_returns_none(self):
        self.assertIsNone(distribution([-1, -1], 100))


class TestParseRate(unittest.TestCase):
    def test_accepts_plain_and_suffixed(self):
        self.assertEqual(parse_rate("8"), 8.0)
        self.assertEqual(parse_rate("8Mbps"), 8.0)
        self.assertEqual(parse_rate(" 2.5 m "), 2.5)

    def test_rejects_nonsense(self):
        for bad in ("", None, "fast", "8MB"):
            with self.assertRaises(Failure):
                parse_rate(bad)


class TestAsList(unittest.TestCase):
    def test_single_object_becomes_a_list(self):
        # PowerShell 5.1 serialises a one-element array as a bare object.
        self.assertEqual(as_list({"a": 1}), [{"a": 1}])

    def test_list_passes_through(self):
        self.assertEqual(as_list([1, 2]), [1, 2])

    def test_none_is_empty(self):
        self.assertEqual(as_list(None), [])


class TestScriptEncoding(unittest.TestCase):
    def test_local_file_has_a_bom(self):
        # PS 5.1 reads a BOM-less .ps1 as ANSI and mangles non-ASCII text.
        path = to_powershell("'ok'\n")
        try:
            with open(path, "rb") as stream:
                self.assertTrue(stream.read().startswith(b"\xef\xbb\xbf"))
        finally:
            os.unlink(path)

    def test_remote_text_has_no_bom(self):
        # `remote run --file` sends the text; a BOM would arrive as U+FEFF and
        # PowerShell would reject the script.
        path = to_powershell("'ok'\n", bom=False)
        try:
            with open(path, "rb") as stream:
                self.assertFalse(stream.read().startswith(b"\xef\xbb\xbf"))
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
