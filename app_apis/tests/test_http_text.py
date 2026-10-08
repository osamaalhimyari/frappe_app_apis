import unittest

import requests

from app_apis.core import http


def _resp(body, ctype="text/html"):
	r = requests.Response()
	r.status_code = 200
	r._content = body.encode()
	r.encoding = "utf-8"
	r.headers["content-type"] = ctype
	r.url = "https://example.test/x"
	return r


class TestHttpText(unittest.TestCase):
	def test_default_cut_and_full_length(self):
		out = http._result(_resp("a" * 250_000))
		self.assertEqual(len(out["text"]), http.MAX_TEXT)
		self.assertEqual(out["text_length"], 250_000)
		self.assertTrue(out["truncated"])

	def test_max_text_returns_more(self):
		out = http._result(_resp("a" * 250_000), max_text=300_000)
		self.assertEqual(len(out["text"]), 250_000)
		self.assertFalse(out["truncated"])

	def test_max_text_is_capped(self):
		self.assertEqual(http._limit(10**9), http.HARD_MAX_TEXT)
		self.assertEqual(http._limit(None), http.MAX_TEXT)

	def test_find_searches_past_the_cut(self):
		body = "a" * 250_000 + "NEEDLE" + "b" * 10
		out = http._result(_resp(body), find="NEEDLE")
		self.assertNotIn("NEEDLE", out["text"])
		self.assertTrue(out["found"])
		self.assertIn("NEEDLE", out["matches"][0])

	def test_find_miss(self):
		out = http._result(_resp("hello"), find="zzz")
		self.assertFalse(out["found"])
		self.assertEqual(out["matches"], [])

	def test_matches_are_limited(self):
		self.assertEqual(len(http._matches("x" * 1000, "x")), http.MAX_MATCHES)
