import gzip
import http.client
import io
import json
import os
import tempfile
import unittest
import urllib.error
from email.message import Message
from unittest import mock

import sundew

URL = "https://archive.test/truth_archive.json"


def _raw(post_id, content):
    return {"id": post_id, "created_at": "2026-10-15T12:00:00Z",
            "content": content, "url": f"https://truth.test/{post_id}",
            "media": []}


class FakeResponse:
    """Just enough of an http.client.HTTPResponse for fetch_posts."""

    def __init__(self, body, headers=None, read_error=None):
        self._body = body
        self._read_error = read_error
        self.headers = Message()
        for k, v in (headers or {}).items():
            self.headers[k] = v

    def read(self):
        if self._read_error:
            raise self._read_error
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def gzip_response(records, etag=None, last_modified=None):
    headers = {"Content-Encoding": "gzip"}
    if etag:
        headers["ETag"] = etag
    if last_modified:
        headers["Last-Modified"] = last_modified
    return FakeResponse(gzip.compress(json.dumps(records).encode()), headers)


def not_modified():
    return urllib.error.HTTPError(URL, 304, "Not Modified", Message(), None)


def sent_headers(urlopen_mock, call=-1):
    req = urlopen_mock.call_args_list[call].args[0]
    return {k.lower(): v for k, v in req.header_items()}


class FetchPostsTests(unittest.TestCase):
    def setUp(self):
        sundew._validators.clear()
        self.addCleanup(sundew._validators.clear)
        log = mock.patch("sundew.log")
        self.log = log.start()
        self.addCleanup(log.stop)

    def test_gzip_response_is_decompressed(self):
        resp = gzip_response([_raw(1, "a &amp; b"), _raw(2, "c")], etag='"v1"')
        with mock.patch("urllib.request.urlopen", return_value=resp) as urlopen:
            posts = sundew.fetch_posts(URL)
        self.assertEqual([p["id"] for p in posts], ["1", "2"])
        self.assertEqual(posts[0]["text"], "a & b")
        self.assertEqual(sent_headers(urlopen)["accept-encoding"], "gzip")
        self.assertEqual(urlopen.call_args.kwargs["timeout"],
                         sundew.config.HTTP_TIMEOUT_SECONDS)
        self.assertIn("(gzip)", self.log.call_args.args[0])

    def test_plain_response_when_server_ignores_accept_encoding(self):
        body = json.dumps([_raw(7, "plain")]).encode()
        resp = FakeResponse(body, {"ETag": '"v1"'})
        with mock.patch("urllib.request.urlopen", return_value=resp):
            posts = sundew.fetch_posts(URL)
        self.assertEqual([p["text"] for p in posts], ["plain"])
        self.assertIn(f"{len(body):,} bytes on the wire (uncompressed)",
                      self.log.call_args.args[0])

    def test_validators_sent_on_second_request(self):
        first = gzip_response([_raw(1, "x")], etag='"v1"',
                              last_modified="Wed, 14 Oct 2026 12:00:00 GMT")
        with mock.patch("urllib.request.urlopen",
                        side_effect=[first, not_modified()]) as urlopen:
            sundew.fetch_posts(URL)
            self.assertNotIn("if-none-match", sent_headers(urlopen, 0))
            with self.assertRaises(sundew.ArchiveUnchanged):
                sundew.fetch_posts(URL)
        second = sent_headers(urlopen, 1)
        self.assertEqual(second["if-none-match"], '"v1"')
        self.assertEqual(second["if-modified-since"],
                         "Wed, 14 Oct 2026 12:00:00 GMT")

    def test_304_keeps_existing_validators(self):
        sundew._validators[URL] = ('"v1"', None)
        with mock.patch("urllib.request.urlopen", side_effect=not_modified()):
            with self.assertRaises(sundew.ArchiveUnchanged):
                sundew.fetch_posts(URL)
        self.assertEqual(sundew._validators[URL], ('"v1"', None))

    def test_other_http_errors_still_raise(self):
        err = urllib.error.HTTPError(URL, 503, "Unavailable", Message(), None)
        self.addCleanup(err.close)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(urllib.error.HTTPError):
                sundew.fetch_posts(URL)

    def assert_validators_untouched(self, failure, expected_exc):
        sundew._validators[URL] = ('"old"', "old-date")
        with mock.patch("urllib.request.urlopen", side_effect=[failure]):
            with self.assertRaises(expected_exc):
                sundew.fetch_posts(URL)
        self.assertEqual(sundew._validators[URL], ('"old"', "old-date"))

    def test_network_error_does_not_update_validators(self):
        self.assert_validators_untouched(
            urllib.error.URLError("connection reset"), urllib.error.URLError)

    def test_incomplete_read_does_not_update_validators(self):
        resp = FakeResponse(b"", {"ETag": '"new"'},
                            read_error=http.client.IncompleteRead(b"[{", 100))
        self.assert_validators_untouched(resp, http.client.IncompleteRead)

    def test_truncated_gzip_does_not_update_validators(self):
        full = gzip.compress(json.dumps([_raw(1, "x")] * 50).encode())
        resp = FakeResponse(full[: len(full) // 2],
                            {"Content-Encoding": "gzip", "ETag": '"new"'})
        self.assert_validators_untouched(resp, EOFError)

    def test_truncated_json_does_not_update_validators(self):
        resp = FakeResponse(b'[{"id": 1, "content": "x"', {"ETag": '"new"'})
        self.assert_validators_untouched(resp, json.JSONDecodeError)

    def test_non_list_does_not_update_validators(self):
        resp = FakeResponse(b'{"error": "nope"}', {"ETag": '"new"'})
        self.assert_validators_untouched(resp, ValueError)

    def test_unexpected_encoding_does_not_update_validators(self):
        resp = FakeResponse(b"\x00\x01", {"Content-Encoding": "br",
                                          "ETag": '"new"'})
        self.assert_validators_untouched(resp, ValueError)


class RunOnceFetchTests(unittest.TestCase):
    def setUp(self):
        sundew._validators.clear()
        self.addCleanup(sundew._validators.clear)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = sundew.Store(os.path.join(temp.name, "sundew.db"))
        self.addCleanup(self.store.close)
        log = mock.patch("sundew.log")
        self.log = log.start()
        self.addCleanup(log.stop)

    def run_pass(self, classify):
        with mock.patch("sundew.classify", classify):
            return sundew.run_once(self.store, {"ANTHROPIC_API_KEY": "unused"},
                                   no_email=True, app_store=None)

    def logged(self):
        return [c.args[0] for c in self.log.call_args_list]

    def test_304_skips_the_whole_pass(self):
        classify = mock.Mock()
        with mock.patch("urllib.request.urlopen", side_effect=not_modified()), \
                mock.patch("sundew.prefilter") as prefilter, \
                mock.patch("sundew.cold_start_backfill") as backfill:
            self.assertEqual(self.run_pass(classify), 0)
        prefilter.assert_not_called()
        backfill.assert_not_called()
        classify.assert_not_called()
        self.assertEqual(self.store.count_seen(), 0)
        self.assertIn("archive unchanged: HTTP 304", self.logged()[-1])
        self.assertFalse(any("fetched" in m for m in self.logged()))

    def test_changed_200_is_processed_and_replaces_validators(self):
        old = _raw(1, "a tariff on steel")
        new = _raw(2, "a tariff on copper")
        first = gzip_response([old], etag='"v1"')
        second = gzip_response([new, old], etag='"v2"')
        classify = mock.Mock(return_value={"category": "other",
                                           "is_noise": True})
        with mock.patch("urllib.request.urlopen",
                        side_effect=[first, second]) as urlopen:
            self.run_pass(classify)
            self.assertEqual(classify.call_count, 1)
            self.run_pass(classify)
        self.assertEqual(sent_headers(urlopen, 1)["if-none-match"], '"v1"')
        self.assertEqual(sundew._validators[sundew.config.ARCHIVE_URL],
                         ('"v2"', None))
        self.assertEqual(classify.call_count, 2)
        self.assertEqual(classify.call_args.args[0]["id"], "2")
        self.assertIn("fetched 2 posts", self.logged())


if __name__ == "__main__":
    unittest.main()
