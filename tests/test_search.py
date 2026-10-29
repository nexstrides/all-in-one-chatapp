"""Search: match modes (substring, whole word, prefix) and the person,
platform and date filters. Plain substring search is covered in test_api."""

import unittest

from aiochat import textmatch
from tests.helpers import AppTestCase, slack_message, telegram_message


class TextMatchTests(unittest.TestCase):
    def test_substring(self):
        self.assertTrue(textmatch.matches("substring", "budget", "rebudgeting"))
        self.assertTrue(textmatch.matches("substring", "BUDGET", "the budget"))
        self.assertFalse(textmatch.matches("substring", "budget", "bud get"))

    def test_word(self):
        m = lambda body: textmatch.matches("word", "budget", body)
        self.assertTrue(m("the budget!"))
        self.assertTrue(m("Budget"))
        self.assertTrue(m("(budget)"))
        self.assertFalse(m("budgets"))
        self.assertFalse(m("rebudget"))
        self.assertFalse(m("budget_v2"), "underscore is a word character")
        self.assertTrue(textmatch.matches("word", "budget review", "the budget review is on"))
        self.assertFalse(textmatch.matches("word", "budget review", "the budget reviews"))

    def test_prefix(self):
        m = lambda body: textmatch.matches("prefix", "bud", body)
        self.assertTrue(m("budget"))
        self.assertTrue(m("a bud"))
        self.assertFalse(m("rebudget"))
        self.assertTrue(textmatch.matches("prefix", "budget rev", "budget review"))

    def test_punctuation_in_query(self):
        # Lookarounds instead of \b: punctuation at the edges of the query
        # must not demand a word character next to it.
        self.assertTrue(textmatch.matches("word", "c++", "I like c++ a lot"))
        self.assertTrue(textmatch.matches("word", "c++", "c++"))
        self.assertFalse(textmatch.matches("word", "c++", "c++11"))
        self.assertTrue(textmatch.matches("word", "@mia", "cc @mia"))
        self.assertFalse(textmatch.matches("word", "@mia", "x@mia"))
        self.assertTrue(textmatch.matches("prefix", "$1", "$100"))
        self.assertFalse(textmatch.matches("word", ".*", "anything"), "regex syntax is literal")

    def test_unicode_words(self):
        self.assertTrue(textmatch.matches("word", "café", "au café!"))
        self.assertFalse(textmatch.matches("word", "caf", "café"))

    def test_none_body_and_unknown_mode(self):
        self.assertFalse(textmatch.matches("word", "x", None))
        self.assertFalse(textmatch.matches("fuzzy", "x", "x"))
        self.assertEqual(textmatch.sql_function("word", "x", "x y"), 1)
        self.assertEqual(textmatch.sql_function("word", "x", "xy"), 0)


class SearchTestCase(AppTestCase):
    def setUp(self):
        super().setUp()
        self.slack = self.create_channel("slack", "Acme", "T1")
        self.telegram = self.create_channel("telegram", "Bot", "bot-1")

    def webhook(self, channel, payload):
        resp = self.send_webhook(channel, payload)
        self.assertEqual(resp.status_code, 200, resp.get_json())

    def search(self, query):
        resp = self.api("GET", f"/api/messages?{query}")
        self.assertEqual(resp.status_code, 200, resp.get_json())
        return resp.get_json()

    def bodies(self, query):
        return [m["body"] for m in self.search(query)["items"]]


class MatchModeTests(SearchTestCase):
    def setUp(self):
        super().setUp()
        self.webhook(self.slack, slack_message(text="Budget review at 3", ts="1761500000.000100"))
        self.webhook(self.slack, slack_message(text="budgets are fine", ts="1761500010.000100"))
        self.webhook(self.slack, slack_message(text="we must rebudget", ts="1761500020.000100"))
        self.webhook(self.slack, slack_message(text="bud, the budget!", ts="1761500030.000100"))

    def test_default_is_substring_and_echoes_match(self):
        result = self.search("q=budget")
        self.assertEqual(result["total"], 4)
        self.assertEqual(result["match"], "substring")
        self.assertEqual(self.search("q=budget&match=substring")["total"], 4)

    def test_whole_word(self):
        result = self.search("q=budget&match=word")
        self.assertEqual(result["match"], "word")
        self.assertEqual([m["body"] for m in result["items"]], ["bud, the budget!", "Budget review at 3"])
        self.assertEqual(self.bodies("q=BUDGET&match=Word"), ["bud, the budget!", "Budget review at 3"])
        self.assertEqual(self.bodies("q=bud&match=word"), ["bud, the budget!"])
        self.assertEqual(self.bodies("q=budget review&match=word"), ["Budget review at 3"])
        self.assertEqual(self.bodies("q=budget rev&match=word"), [])

    def test_prefix(self):
        self.assertEqual(
            self.bodies("q=bud&match=prefix"),
            ["bud, the budget!", "budgets are fine", "Budget review at 3"],
        )
        self.assertEqual(self.bodies("q=budget rev&match=prefix"), ["Budget review at 3"])
        self.assertEqual(self.bodies("q=rebud&match=prefix"), ["we must rebudget"])

    def test_stricter_modes_are_subsets_of_substring(self):
        for q in ("bud", "budget", "BUDGET!", "e"):
            substring = set(self.bodies(f"q={q}"))
            self.assertLessEqual(set(self.bodies(f"q={q}&match=word")), substring, q)
            self.assertLessEqual(set(self.bodies(f"q={q}&match=prefix")), substring, q)

    def test_like_wildcards_stay_literal_in_every_mode(self):
        self.webhook(self.slack, slack_message(text="100% done", ts="1761500040.000100"))
        self.webhook(self.slack, slack_message(text="100 done", ts="1761500050.000100"))
        for mode in ("substring", "word", "prefix"):
            self.assertEqual(self.bodies(f"q=100%25&match={mode}"), ["100% done"], mode)
            self.assertEqual(self.search(f"q=_&match={mode}")["total"], 0, mode)

    def test_pagination_in_word_mode(self):
        page2 = self.search("q=budget&match=word&page_size=1&page=2")
        self.assertEqual(page2["total"], 2)
        self.assertEqual([m["body"] for m in page2["items"]], ["Budget review at 3"])

    def test_invalid_mode(self):
        resp = self.api("GET", "/api/messages?q=budget&match=fuzzy")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"]["code"], "invalid_parameter")


class SearchFilterTests(SearchTestCase):
    def setUp(self):
        super().setUp()
        # Mia on Slack (T0) and Telegram (T2); Omar on Slack (T1); our reply.
        self.webhook(self.slack, slack_message(user="U-MIA", text="budget review", ts="1761500000.000100",
                                               user_profile={"display_name": "Mia"}))
        self.webhook(self.slack, slack_message(user="U-OMAR", text="budget is tight", ts="1761500050.000100",
                                               user_profile={"display_name": "Omar"}))
        self.webhook(self.telegram, telegram_message(message_id=1, chat_id=9002, user_id=9002,
                                                     text="budget notes?", date=1761500100))
        [conv] = self.api("GET", f"/api/conversations?channel_id={self.telegram['id']}").get_json()["items"]
        self.api("POST", f"/api/conversations/{conv['id']}/messages", json={"body": "budget sent"})
        contacts = {c["display_name"]: c for c in self.api("GET", "/api/contacts").get_json()["items"]}
        resp = self.api("POST", "/api/people", json={"name": "Mia", "contact_ids": [contacts["Mia"]["id"], contacts["Ana Silva"]["id"]]})
        self.person = resp.get_json()

    def test_person(self):
        result = self.search(f"q=budget&person_id={self.person['id']}")
        self.assertEqual([m["body"] for m in result["items"]], ["budget notes?", "budget review"])
        self.assertEqual([m["conversation"]["platform"] for m in result["items"]], ["telegram", "slack"])
        self.assertEqual(self.api("GET", "/api/messages?q=budget&person_id=999").status_code, 404)

    def test_platform(self):
        self.assertEqual(self.bodies("q=budget&platform=slack"), ["budget is tight", "budget review"])
        self.assertEqual(self.bodies("q=budget&platform=Telegram"), ["budget sent", "budget notes?"])
        resp = self.api("GET", "/api/messages?q=budget&platform=fax")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"]["code"], "invalid_parameter")

    def test_date_bounds(self):
        # T0 = 2025-10-26T17:33:20Z, T1 = 17:34:10Z, T2 = 17:35:00Z; the reply is "now".
        self.assertEqual(self.bodies("q=budget&since=2025-10-26T17:34:10Z"), ["budget sent", "budget notes?", "budget is tight"])
        self.assertEqual(self.bodies("q=budget&since=2025-10-26T17:34:10.001Z"), ["budget sent", "budget notes?"])
        self.assertEqual(self.bodies("q=budget&until=2025-10-26T17:34:10Z"), ["budget is tight", "budget review"])
        self.assertEqual(self.bodies("q=budget&until=2025-10-26T17:34:09"), ["budget review"], "naive means UTC")
        self.assertEqual(self.bodies("q=budget&until=2025-10-26T18:34:10%2B01:00"), ["budget is tight", "budget review"])
        self.assertEqual(self.bodies("q=budget&since=2025-10-26T17:34:10Z&until=2025-10-26T17:35:00Z"),
                         ["budget notes?", "budget is tight"])

    def test_bare_dates_cover_whole_days(self):
        self.assertEqual(self.search("q=budget&since=2025-10-26&until=2025-10-26")["total"], 3)
        self.assertEqual(self.search("q=budget&until=2025-10-25")["total"], 0)
        self.assertEqual(self.search("q=budget&since=2025-10-27")["total"], 1, "only the reply made now")
        self.assertEqual(self.search("q=budget&since=2025-10-26&until=")["total"], 4, "blank means unset")

    def test_invalid_dates(self):
        for value in ("yesterday", "2025-13-01", "2025-10-26T25:00:00Z", "26/10/2025"):
            resp = self.api("GET", f"/api/messages?q=budget&since={value}")
            self.assertEqual(resp.status_code, 400, value)
            self.assertEqual(resp.get_json()["error"]["code"], "invalid_parameter")
        self.assertEqual(self.api("GET", "/api/messages?q=budget&until=nope").status_code, 400)

    def test_filters_combine_with_match_mode(self):
        self.webhook(self.slack, slack_message(user="U-MIA", text="budgets!", ts="1761500200.000100",
                                               user_profile={"display_name": "Mia"}))
        q = f"q=budget&person_id={self.person['id']}&platform=slack&since=2025-10-26&until=2025-10-26"
        self.assertEqual(self.bodies(q), ["budgets!", "budget review"])
        self.assertEqual(self.bodies(f"{q}&match=word"), ["budget review"])
        self.assertEqual(self.bodies(f"{q}&match=prefix"), ["budgets!", "budget review"])

    def test_hits_carry_conversation_and_sender(self):
        [hit] = self.search(f"q=review&person_id={self.person['id']}")["items"]
        self.assertEqual(hit["conversation"], {"id": hit["conversation_id"], "title": "C1", "platform": "slack"})
        self.assertEqual(hit["sender"]["display_name"], "Mia")


if __name__ == "__main__":
    unittest.main()
