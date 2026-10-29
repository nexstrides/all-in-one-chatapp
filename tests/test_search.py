"""Full-text search: word and prefix matching, the person, platform and date
filters, and keeping the FTS5 index in step with the messages table."""

import unittest

from aiochat import textmatch
from aiochat.db import get_db
from tests.helpers import AppTestCase, slack_changed, slack_deleted, slack_message, telegram_message


class FtsQueryTests(unittest.TestCase):
    def test_words(self):
        self.assertEqual(textmatch.words("  notes   standup "), ["notes", "standup"])
        self.assertEqual(textmatch.words("c++ @mia 100%"), ["c++", "@mia", "100%"])
        self.assertEqual(textmatch.words("_ % ... \"\""), [], "no letters or digits, no words")

    def test_word_mode_quotes_every_term(self):
        self.assertEqual(textmatch.fts_query("notes standup"), '"notes" "standup"')
        self.assertEqual(textmatch.fts_query("Budget", "word"), '"Budget"')

    def test_prefix_mode_marks_every_term(self):
        self.assertEqual(textmatch.fts_query("note stand", "prefix"), '"note"* "stand"*')

    def test_operators_and_quotes_are_literal(self):
        # FTS5 syntax typed by the user must not be interpreted.
        self.assertEqual(textmatch.fts_query("budget OR cats"), '"budget" "OR" "cats"')
        self.assertEqual(textmatch.fts_query('say "hi" NOT'), '"say" """hi""" "NOT"')
        self.assertEqual(textmatch.fts_query("a* (b)"), '"a*" "(b)"')

    def test_no_words_means_no_query(self):
        self.assertIsNone(textmatch.fts_query("_"))
        self.assertIsNone(textmatch.fts_query("%%%", "prefix"))


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
        texts = [
            "Can you send me the standup notes?",
            "Budget review at 3",
            "budgets are fine",
            "we must rebudget",
            "bud, the budget!",
            "budget-review tomorrow",
            "au café, c++ and @mia",
        ]
        for i, text in enumerate(texts):
            self.webhook(self.slack, slack_message(text=text, ts=f"{1761500000 + i}.000100"))

    def test_every_word_must_match_in_any_order(self):
        self.assertEqual(self.bodies("q=notes%20standup"), ["Can you send me the standup notes?"])
        self.assertEqual(self.bodies("q=standup%20notes"), ["Can you send me the standup notes?"])
        self.assertEqual(self.bodies("q=standup%20notes%20budget"), [], "one word missing is no hit")
        self.assertEqual(self.search("q=the")["total"], 2)
        self.assertEqual(self.bodies("q=the%20budget"), ["bud, the budget!"])

    def test_default_is_whole_word(self):
        result = self.search("q=budget")
        self.assertEqual(result["match"], "word")
        self.assertEqual(
            [m["body"] for m in result["items"]],
            ["budget-review tomorrow", "bud, the budget!", "Budget review at 3"],
        )
        self.assertEqual(self.bodies("q=note"), [], "note is not the word notes")
        self.assertEqual(self.bodies("q=bud"), ["bud, the budget!"])
        self.assertEqual(self.bodies("q=budget&match=word"), self.bodies("q=budget"))

    def test_prefix(self):
        result = self.search("q=note%20stand&match=prefix")
        self.assertEqual(result["match"], "prefix")
        self.assertEqual([m["body"] for m in result["items"]], ["Can you send me the standup notes?"])
        self.assertEqual(
            self.bodies("q=bud&match=prefix"),
            ["budget-review tomorrow", "bud, the budget!", "budgets are fine", "Budget review at 3"],
        )
        self.assertEqual(self.bodies("q=rebud&match=prefix"), ["we must rebudget"])
        self.assertEqual(self.bodies("q=udget&match=prefix"), [], "prefix, not substring")

    def test_case_and_accents_are_folded(self):
        self.assertEqual(self.search("q=BUDGET%20REVIEW")["total"], 2)
        self.assertEqual(self.bodies("q=cafe"), ["au café, c++ and @mia"])
        self.assertEqual(self.bodies("q=CAFÉ"), ["au café, c++ and @mia"])

    def test_punctuation_is_not_part_of_words(self):
        # A hyphenated query is the phrase "budget review" (adjacent words).
        self.assertEqual(self.bodies("q=budget-review"), ["budget-review tomorrow", "Budget review at 3"])
        self.assertEqual(self.bodies("q=review-budget"), [])
        self.assertEqual(self.bodies("q=@mia"), ["au café, c++ and @mia"])
        self.assertEqual(self.bodies("q=c%2B%2B"), ["au café, c++ and @mia"])
        self.assertEqual(self.bodies("q=budget!"), self.bodies("q=budget"))

    def test_query_without_words_matches_nothing(self):
        for q in ("_", "%25", "...", "%22%22"):
            result = self.search(f"q={q}")
            self.assertEqual(result["total"], 0, q)
            self.assertEqual(result["items"], [])
            self.assertEqual(result["match"], "word")

    def test_fts_syntax_in_the_query_is_literal(self):
        self.assertEqual(self.search("q=budget%20OR%20standup")["total"], 0, "OR is just a missing word")
        self.assertEqual(self.search("q=budget%20NOT%20review")["total"], 0)
        self.assertEqual(self.search("q=bud*")["total"], 1, "* is not a wildcard in word mode")
        self.assertEqual(self.search("q=%22budget%20review%22")["total"], 2, "quotes are ignored")
        self.assertEqual(self.search("q=body:budget")["total"], 0, "column filters are not available")
        self.assertEqual(self.search("q=(budget")["total"], 3)

    def test_pagination(self):
        page2 = self.search("q=budget&page_size=2&page=2")
        self.assertEqual(page2["total"], 3)
        self.assertEqual([m["body"] for m in page2["items"]], ["Budget review at 3"])

    def test_invalid_mode(self):
        for mode in ("fuzzy", "substring"):
            resp = self.api("GET", f"/api/messages?q=budget&match={mode}")
            self.assertEqual(resp.status_code, 400, mode)
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

    def test_channel(self):
        self.assertEqual(self.bodies(f"q=budget&channel_id={self.slack['id']}"), ["budget is tight", "budget review"])
        self.assertEqual(self.bodies(f"q=budget&channel_id={self.telegram['id']}"), ["budget sent", "budget notes?"])
        # Like channel_id on /api/conversations: an unknown channel is an empty result, not a 404.
        result = self.search("q=budget&channel_id=999")
        self.assertEqual((result["total"], result["items"]), (0, []))
        for bad in ("0", "-1", "abc"):
            resp = self.api("GET", f"/api/messages?q=budget&channel_id={bad}")
            self.assertEqual(resp.status_code, 400, bad)
            self.assertEqual(resp.get_json()["error"]["code"], "invalid_parameter")

    def test_channel_distinguishes_accounts_on_the_same_platform(self):
        other_slack = self.create_channel("slack", "Other workspace", "T2")
        self.webhook(other_slack, slack_message(channel="C9", user="U-ZOE", text="budget from T2", ts="1761500300.000100"))
        self.assertEqual(self.bodies(f"q=budget&channel_id={other_slack['id']}"), ["budget from T2"])
        self.assertEqual(self.bodies(f"q=budget&channel_id={self.slack['id']}"), ["budget is tight", "budget review"])
        self.assertEqual(self.search("q=budget&platform=slack")["total"], 3, "platform spans both workspaces")

    def test_channel_combines_with_platform_and_person(self):
        # Agreeing filters narrow; contradicting ones yield nothing.
        self.assertEqual(self.bodies(f"q=budget&channel_id={self.slack['id']}&platform=slack"), ["budget is tight", "budget review"])
        self.assertEqual(self.search(f"q=budget&channel_id={self.slack['id']}&platform=telegram")["total"], 0)
        self.assertEqual(self.bodies(f"q=budget&channel_id={self.slack['id']}&person_id={self.person['id']}"), ["budget review"])
        self.assertEqual(self.bodies(f"q=bud&match=prefix&channel_id={self.telegram['id']}&since=2025-10-26&until=2025-10-26"),
                         ["budget notes?"])

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
        self.assertEqual(self.bodies(q), ["budget review"])
        self.assertEqual(self.bodies(f"{q}&match=prefix"), ["budgets!", "budget review"])

    def test_hits_carry_conversation_and_sender(self):
        [hit] = self.search(f"q=review&person_id={self.person['id']}")["items"]
        self.assertEqual(hit["conversation"], {"id": hit["conversation_id"], "title": "C1", "platform": "slack"})
        self.assertEqual(hit["sender"]["display_name"], "Mia")


class IndexSyncTests(SearchTestCase):
    """The FTS index must follow every way a message row can change."""

    def setUp(self):
        super().setUp()
        # Every body contains "msg", so MATCH 'msg' should list exactly the
        # ids in the messages table whenever the index is in step.
        self.webhook(self.slack, slack_message(channel="C1", text="msg one", ts="1761500000.000100"))
        self.webhook(self.slack, slack_message(channel="C1", text="msg two", ts="1761500001.000100"))
        self.webhook(self.telegram, telegram_message(message_id=1, text="msg three", date=1761500002))

    def indexed_ids(self):
        with self.app.app_context():
            db = get_db()
            indexed = sorted(r[0] for r in db.execute("SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'msg'"))
            stored = sorted(r[0] for r in db.execute("SELECT id FROM messages"))
        return indexed, stored

    def assert_in_step(self):
        indexed, stored = self.indexed_ids()
        self.assertEqual(indexed, stored)

    def test_insert_edit_and_reply(self):
        self.assert_in_step()
        conv_id = self.search("q=one")["items"][0]["conversation_id"]
        self.api("POST", f"/api/conversations/{conv_id}/messages", json={"body": "msg reply"})
        self.assertEqual(self.search("q=reply")["total"], 1)
        self.webhook(self.slack, slack_changed(ts="1761500000.000100", text="msg uno"))
        self.assertEqual(self.search("q=one")["total"], 0)
        self.assertEqual(self.search("q=uno")["total"], 1)
        self.assert_in_step()

    def test_soft_delete_hides_and_purge_unindexes(self):
        self.webhook(self.slack, slack_deleted(deleted_ts="1761500000.000100"))
        self.assertEqual(self.search("q=one")["total"], 0, "soft-deleted messages are hidden")
        indexed, stored = self.indexed_ids()
        self.assertEqual(indexed, stored, "but still indexed, like the row is still stored")

        from aiochat.services.inbox import purge_deleted
        with self.app.app_context():
            get_db().execute("UPDATE messages SET deleted_at = '2020-01-01T00:00:00.000Z' WHERE body = 'msg one'")
            get_db().commit()
            self.assertEqual(purge_deleted(retention_days=1), 1)
        self.assert_in_step()
        self.assertEqual(self.search("q=msg")["total"], 2)

    def test_channel_delete_cascades_into_the_index(self):
        self.api("DELETE", f"/api/channels/{self.slack['id']}")
        self.assert_in_step()
        self.assertEqual(self.bodies("q=msg"), ["msg three"])

    def test_reindex_command_repairs_a_stale_index(self):
        with self.app.app_context():
            db = get_db()
            db.execute("DROP TRIGGER messages_fts_insert")
            db.execute(
                "INSERT INTO messages (conversation_id, direction, body, sent_at)"
                " VALUES (1, 'outbound', 'msg unindexed', '2025-10-26T18:00:00.000Z')"
            )
            db.commit()
        self.assertEqual(self.search("q=unindexed")["total"], 0)
        result = self.app.test_cli_runner().invoke(args=["reindex-search"])
        self.assertEqual(result.exit_code, 0, result.output or repr(result.exception))
        self.assertIn("indexed=4", result.output)
        self.assertEqual(self.search("q=unindexed")["total"], 1)
        self.assert_in_step()


if __name__ == "__main__":
    unittest.main()
