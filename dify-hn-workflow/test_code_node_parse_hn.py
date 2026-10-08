# -*- coding: utf-8 -*-
"""
code_node_parse_hn.py 的测试用例。

运行方式（在本目录下）:
    python -m unittest test_code_node_parse_hn -v
或在仓库根目录:
    python -m unittest discover -s dify-hn-workflow -v
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from code_node_parse_hn import (  # noqa: E402
    build_story,
    dedupe_key,
    extract_source,
    format_time,
    format_time_utc,
    host_of,
    main,
    normalize_url,
    parse_hn_json,
    to_unix_seconds,
)

# 1791330005 == 2026-10-06T23:40:05Z == 北京时间 2026-10-07 07:40
EPOCH_SECONDS = 1791330005

CORE_FIELDS = {"title", "url", "time", "source"}
ALL_FIELDS = CORE_FIELDS | {
    "id", "author", "score", "comments", "text", "time_iso", "time_unix",
}

# Algolia 的 front_page 接口返回形状（最常用的一次请求拿全文）
ALGOLIA_PAYLOAD = {
    "hits": [
        {
            "objectID": "41234567",
            "title": "Show HN:  I built a tiny workflow engine",
            "url": "https://example.com/posts/workflow?utm_source=hn&utm_medium=social",
            "created_at": "2026-10-06T23:40:05Z",
            "created_at_i": EPOCH_SECONDS,
            "author": "alice",
            "points": 321,
            "num_comments": 87,
        },
        {
            "objectID": "41234568",
            "title": "Ask HN: How do you keep up with AI news?",
            "url": None,
            "created_at_i": 1791333605,
            "author": "bob",
            "points": 56,
        },
        {
            "objectID": "41234569",
            "title": "  Show HN: I built a tiny workflow engine  ",
            "url": "https://www.example.com/posts/workflow/?utm_campaign=weekly",
            "created_at_i": EPOCH_SECONDS,
        },
        {
            "objectID": "41234570",
            "title": "",
            "url": "https://example.com/broken",
        },
        {
            "title": "Rust 1.90 released",
            "url": "https://blog.rust-lang.org/2026/10/05/Rust-1.90.0.html",
            "time": None,
            "score": "410",
        },
    ]
}

# Firebase 官方 API 的单个 item 形状
FIREBASE_ITEMS = {
    "41234567": {
        "id": 41234567,
        "by": "alice",
        "title": "Rust 1.90 released",
        "url": "https://blog.rust-lang.org/2026/10/05/Rust-1.90.0.html",
        "score": 410,
        "descendants": 120,
        "time": EPOCH_SECONDS,
        "type": "story",
    },
    "41234568": {
        "id": 41234568,
        "by": "bob",
        "title": "Ask HN: How do you keep up with AI news?",
        "score": 56,
        "time": EPOCH_SECONDS,
        "type": "story",
    },
    "41234569": {
        "id": 41234569,
        "by": "carol",
        "text": "招聘贴，没有标题也没有链接",
        "time": EPOCH_SECONDS,
        "type": "comment",
    },
}


class TestCollectAndShape(unittest.TestCase):
    """输入形状兼容性。"""

    def test_json_string_input_is_accepted(self):
        result = parse_hn_json(json.dumps(ALGOLIA_PAYLOAD, ensure_ascii=False))
        self.assertEqual(3, result["count"])
        self.assertEqual("", result["error"])

    def test_bare_list_input_is_accepted(self):
        result = parse_hn_json(ALGOLIA_PAYLOAD["hits"])
        self.assertEqual(3, result["count"])

    def test_nested_data_key_is_unwrapped(self):
        result = parse_hn_json({"data": {"stories": ALGOLIA_PAYLOAD["hits"]}})
        self.assertEqual(3, result["count"])

    def test_firebase_items_dict_is_accepted(self):
        result = parse_hn_json(FIREBASE_ITEMS)
        self.assertEqual(2, result["count"])          # 无标题的 comment 被丢弃
        self.assertEqual(1, result["skipped"])

    def test_single_item_object_is_accepted(self):
        result = parse_hn_json(FIREBASE_ITEMS["41234567"])
        self.assertEqual(1, result["count"])

    def test_topstories_id_list_yields_nothing_but_does_not_crash(self):
        # 官方 topstories.json 只返回 id 列表，本节点无法自行联网补全详情
        result = parse_hn_json([41234567, 41234568])
        self.assertEqual(0, result["count"])
        self.assertEqual(2, result["skipped"])


class TestFieldExtraction(unittest.TestCase):
    """输出字段、空值回填与来源识别。"""

    def test_every_story_has_core_and_extra_fields(self):
        for story in parse_hn_json(ALGOLIA_PAYLOAD)["stories"]:
            self.assertEqual(ALL_FIELDS, set(story))

    def test_title_whitespace_is_collapsed(self):
        story = parse_hn_json(ALGOLIA_PAYLOAD)["stories"][0]
        self.assertEqual("Show HN: I built a tiny workflow engine", story["title"])

    def test_missing_url_falls_back_to_hn_item_page(self):
        stories = parse_hn_json(FIREBASE_ITEMS)["stories"]
        ask = [s for s in stories if s["title"].startswith("Ask HN")][0]
        self.assertEqual("https://news.ycombinator.com/item?id=41234568", ask["url"])
        self.assertEqual("Hacker News", ask["source"])

    def test_score_string_is_coerced_to_int(self):
        stories = parse_hn_json(ALGOLIA_PAYLOAD)["stories"]
        rust = [s for s in stories if s["title"].startswith("Rust")][0]
        self.assertEqual(410, rust["score"])

    def test_missing_score_and_comments_stay_none(self):
        stories = parse_hn_json(ALGOLIA_PAYLOAD)["stories"]
        rust = [s for s in stories if s["title"].startswith("Rust")][0]
        self.assertIsNone(rust["comments"])

    def test_story_text_is_kept_and_truncated(self):
        stories = parse_hn_json({"id": 1, "title": "T", "url": "https://a.com", "story_text": "x" * 900})["stories"]
        self.assertEqual(400, len(stories[0]["text"]))

    def test_missing_story_text_is_empty_string(self):
        story = parse_hn_json(ALGOLIA_PAYLOAD)["stories"][0]
        self.assertEqual("", story["text"])

    def test_explicit_source_field_wins(self):
        self.assertEqual("arxiv.org", extract_source({"source": "arxiv.org"}, "https://x.com/a"))

    def test_source_is_derived_from_domain(self):
        self.assertEqual("twitter.com", extract_source({}, "https://x.com/a/b"))

    def test_source_falls_back_to_hacker_news(self):
        self.assertEqual("Hacker News", extract_source({}, ""))

    def test_empty_and_non_dict_items_return_none(self):
        self.assertIsNone(build_story({}))
        self.assertIsNone(build_story({"title": "   "}))
        self.assertIsNone(build_story({"title": True, "url": "https://a.com"}))
        self.assertIsNone(build_story({"title": "no link and no id"}))
        self.assertIsNone(build_story(None))
        self.assertIsNone(build_story("not-a-dict"))

    def test_output_keeps_input_order(self):
        titles = [s["title"] for s in parse_hn_json(ALGOLIA_PAYLOAD)["stories"]]
        self.assertEqual(
            [
                "Show HN: I built a tiny workflow engine",
                "Ask HN: How do you keep up with AI news?",
                "Rust 1.90 released",
            ],
            titles,
        )


class TestDedupe(unittest.TestCase):
    """去重逻辑。"""

    def test_tracking_params_and_www_are_ignored_for_dedupe(self):
        result = parse_hn_json(ALGOLIA_PAYLOAD)
        self.assertEqual(3, result["count"])
        self.assertEqual(1, result["deduplicated"])

    def test_meaningful_query_params_are_kept(self):
        payload = [
            {"title": "A", "url": "https://example.com/post?id=1"},
            {"title": "B", "url": "https://example.com/post?id=2"},
        ]
        self.assertEqual(2, parse_hn_json(payload)["count"])

    def test_duplicate_keeps_first_url_and_fills_missing_fields(self):
        payload = [
            {"id": 99, "title": "Same story", "url": "https://example.com/a?utm_source=x", "time": EPOCH_SECONDS},
            {"id": 99, "title": "Same story (dup)", "url": "https://www.example.com/a/", "author": "bob"},
        ]
        stories = parse_hn_json(payload)["stories"]
        self.assertEqual(1, len(stories))
        self.assertEqual("https://example.com/a?utm_source=x", stories[0]["url"])
        self.assertEqual("example.com", stories[0]["source"])
        self.assertEqual("bob", stories[0]["author"])
        self.assertEqual(EPOCH_SECONDS, stories[0]["time_unix"])

    def test_items_with_same_id_are_deduped_and_gaps_are_filled(self):
        payload = [
            {"id": 42, "title": "Same story", "time": EPOCH_SECONDS},
            {"id": 42, "title": "Same story (duplicate)", "author": "bob", "time": EPOCH_SECONDS},
        ]
        result = parse_hn_json(payload)
        self.assertEqual(1, result["count"])
        self.assertEqual(1, result["deduplicated"])
        self.assertEqual("Same story", result["stories"][0]["title"])   # 保留先出现的
        self.assertEqual("bob", result["stories"][0]["author"])         # 补齐缺失字段

    def test_dedupe_key_falls_back_to_title_when_url_is_empty(self):
        left = {"title": "Same  Title", "url": ""}
        right = {"title": "same title", "url": ""}
        self.assertEqual(dedupe_key(left), dedupe_key(right))

    def test_deduplicate_counter_and_skipped_counter_together(self):
        result = parse_hn_json(ALGOLIA_PAYLOAD)
        self.assertEqual(
            {"count": 3, "skipped": 1, "deduplicated": 1},
            {k: result[k] for k in ("count", "skipped", "deduplicated")},
        )


class TestUrlHelpers(unittest.TestCase):
    """URL 归一化与域名提取。"""

    def test_normalize_url_variants(self):
        cases = {
            "https://example.com/posts/workflow?utm_source=hn&utm_medium=social": "example.com/posts/workflow",
            "https://www.example.com/posts/workflow/?utm_campaign=weekly": "example.com/posts/workflow",
            "http://example.com/redirect?ref=hn&id=7": "example.com/redirect?id=7",
            "HTTPS://Example.COM/Path?b=2&a=1": "example.com/Path?a=1&b=2",
            "example.com/bare": "example.com/bare",
            "https://example.com:8443/a/": "example.com:8443/a",
            "https://example.com/a#section": "example.com/a",
            "": "",
            None: "",
        }
        for raw, expected in cases.items():
            self.assertEqual(expected, normalize_url(raw), msg="input=%r" % (raw,))

    def test_host_of_variants(self):
        cases = {
            "https://www.github.com/a": "github.com",
            "https://m.example.com/x": "example.com",
            "https://x.com/a": "twitter.com",
            "https://old.reddit.com/r/x": "reddit.com",
            "https://news.ycombinator.com/item?id=1": "news.ycombinator.com",
            "example.com/bare": "example.com",
            "": "",
            None: "",
        }
        for raw, expected in cases.items():
            self.assertEqual(expected, host_of(raw), msg="input=%r" % (raw,))

    def test_normalize_url_strips_host_prefixes_and_aliases(self):
        self.assertEqual("example.com/a", normalize_url("https://m.example.com/a"))
        self.assertEqual("example.com/a", normalize_url("https://AMP.example.com/a"))
        self.assertEqual("twitter.com/x", normalize_url("https://x.com/x"))
        self.assertEqual("twitter.com/x", normalize_url("https://twitter.com/x/"))


class TestTimeHandling(unittest.TestCase):
    """时间解析与格式化。"""

    def test_epoch_seconds(self):
        self.assertEqual("2026-10-07 07:40", format_time(EPOCH_SECONDS, 8))
        self.assertEqual("2026-10-06 23:40", format_time(EPOCH_SECONDS, 0))

    def test_epoch_milliseconds_microseconds_nanoseconds(self):
        for value in (
            str(EPOCH_SECONDS * 1000),
            EPOCH_SECONDS * 1_000_000,
            EPOCH_SECONDS * 1_000_000_000,
        ):
            self.assertEqual("2026-10-06 23:40", format_time(value, 0), msg="input=%r" % (value,))

    def test_iso8601_variants(self):
        cases = {
            "2026-10-06T23:40:05Z": "2026-10-06 23:40",
            "2026-10-07T07:40:05+08:00": "2026-10-06 23:40",
            "2026-10-06T23:40:05": "2026-10-06 23:40",
            "2026-10-06": "2026-10-06 00:00",
        }
        for raw, expected in cases.items():
            self.assertEqual(expected, format_time(raw, 0), msg="input=%r" % (raw,))

    def test_unparsable_time_becomes_empty_string(self):
        for value in (None, "", "   ", "not-a-date", "abc123", True, {}, []):
            self.assertEqual("", format_time(value, 0), msg="input=%r" % (value,))

    def test_out_of_range_time_becomes_empty_string(self):
        self.assertEqual("", format_time(10 ** 20, 0))

    def test_custom_format_and_timezone(self):
        self.assertEqual(
            "2026/10/07",
            format_time(EPOCH_SECONDS, 8, "%Y/%m/%d"),
        )
        self.assertEqual(
            "2026-10-06 23:40:05",
            format_time(EPOCH_SECONDS, 0, "%Y-%m-%d %H:%M:%S"),
        )
        self.assertEqual(
            "2026-10-07 05:10",
            format_time(EPOCH_SECONDS, 5.5),
        )

    def test_iso_utc_output(self):
        self.assertEqual("2026-10-06T23:40:05Z", format_time_utc(EPOCH_SECONDS))
        self.assertEqual("", format_time_utc(None))

    def test_to_unix_seconds(self):
        self.assertEqual(EPOCH_SECONDS, to_unix_seconds(EPOCH_SECONDS))
        self.assertEqual(EPOCH_SECONDS, to_unix_seconds(str(EPOCH_SECONDS)))
        self.assertEqual(EPOCH_SECONDS, to_unix_seconds("2026-10-06T23:40:05Z"))
        self.assertIsNone(to_unix_seconds(""))
        self.assertIsNone(to_unix_seconds(None))
        self.assertIsNone(to_unix_seconds(False))

    def test_story_time_fields(self):
        story = parse_hn_json(ALGOLIA_PAYLOAD)["stories"][0]
        self.assertEqual("2026-10-07 07:40", story["time"])
        self.assertEqual("2026-10-06T23:40:05Z", story["time_iso"])
        self.assertEqual(EPOCH_SECONDS, story["time_unix"])

    def test_story_without_time_keeps_empty_strings(self):
        stories = parse_hn_json(ALGOLIA_PAYLOAD)["stories"]
        rust = [s for s in stories if s["title"].startswith("Rust")][0]
        self.assertEqual("", rust["time"])
        self.assertEqual("", rust["time_iso"])
        self.assertIsNone(rust["time_unix"])


class TestOptionsAndRobustness(unittest.TestCase):
    """参数开关与异常兜底。"""

    def test_max_items_limits_output(self):
        result = main(json.dumps(ALGOLIA_PAYLOAD), max_items=2)
        self.assertEqual(2, result["count"])

    def test_max_items_zero_means_no_limit(self):
        self.assertEqual(3, main(ALGOLIA_PAYLOAD, max_items=0)["count"])

    def test_max_items_accepts_string(self):
        self.assertEqual(1, main(ALGOLIA_PAYLOAD, max_items="1")["count"])

    def test_invalid_timezone_value_falls_back_to_utc(self):
        self.assertEqual("2026-10-06 23:40", main(ALGOLIA_PAYLOAD, tz_offset_hours="abc")["stories"][0]["time"])

    def test_malformed_json_string_returns_empty_result(self):
        result = main("not json at all")
        self.assertEqual([], result["stories"])
        self.assertEqual(0, result["count"])
        self.assertEqual("", result["error"])

    def test_unexpected_input_types_never_raise(self):
        for payload in (None, 12345, object(), {"foo": "bar"}, [], ""):
            result = main(payload)
            self.assertEqual(0, result["count"], msg="input=%r" % (payload,))
            self.assertEqual("", result["error"], msg="input=%r" % (payload,))

    def test_output_is_json_serializable(self):
        json.dumps(main(ALGOLIA_PAYLOAD), ensure_ascii=False)

    def test_dify_declared_output_keys_are_stable(self):
        self.assertEqual(
            {"stories", "count", "skipped", "deduplicated", "error"},
            set(main(ALGOLIA_PAYLOAD)),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
