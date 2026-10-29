"""People: grouping contacts across platforms by hand, and what that gives
you (unread count, conversation filter, unified timeline)."""

import unittest

from tests.helpers import AppTestCase, slack_deleted, slack_message, telegram_message

T0 = "1761500000.000100"  # 2025-10-26T17:33:20Z
T1 = "1761500050.000100"
T2 = "1761500100.000100"


def slack_from(user, name, text, ts, channel="C1", **event):
    return slack_message(
        channel=channel, user=user, text=text, ts=ts, user_profile={"display_name": name}, **event
    )


class PeopleTestCase(AppTestCase):
    """Mia writes on Slack (as U-MIA) and Telegram (as user 9002); Omar is
    Slack-only and shares the #general channel with her."""

    def setUp(self):
        super().setUp()
        self.slack = self.create_channel("slack", "Acme", "T1")
        self.telegram = self.create_channel("telegram", "Bot", "bot-1")
        self.webhook(self.slack, slack_from("U-MIA", "Mia", "standup in 10?", T0))
        self.webhook(self.slack, slack_from("U-OMAR", "Omar", "running late", T1))
        self.webhook(self.telegram, telegram_message(message_id=1, chat_id=9002, user_id=9002,
                                                     text="send me the notes?", date=1761500100))
        contacts = self.api("GET", "/api/contacts").get_json()["items"]
        by_key = {(c["platform"], c["external_user_id"]): c for c in contacts}
        self.mia_slack = by_key[("slack", "U-MIA")]
        self.mia_telegram = by_key[("telegram", "9002")]
        self.omar = by_key[("slack", "U-OMAR")]

    def webhook(self, channel, payload):
        resp = self.send_webhook(channel, payload)
        self.assertEqual(resp.status_code, 200, resp.get_json())
        return resp.get_json()

    def create_person(self, name="Mia", contact_ids=None):
        body = {"name": name}
        if contact_ids is not None:
            body["contact_ids"] = contact_ids
        resp = self.api("POST", "/api/people", json=body)
        self.assertEqual(resp.status_code, 201, resp.get_json())
        return resp.get_json()

    def mia(self):
        return self.create_person("Mia", [self.mia_slack["id"], self.mia_telegram["id"]])

    def conversation_id(self, channel):
        [conv] = self.api("GET", f"/api/conversations?channel_id={channel['id']}").get_json()["items"]
        return conv["id"]


class ContactTests(PeopleTestCase):
    def test_lists_contacts_with_platform_and_no_person(self):
        result = self.api("GET", "/api/contacts").get_json()
        self.assertEqual(result["total"], 3)
        self.assertEqual([c["display_name"] for c in result["items"]], ["Ana Silva", "Mia", "Omar"])
        self.assertEqual(self.mia_slack["platform"], "slack")
        self.assertEqual(self.mia_slack["channel_id"], self.slack["id"])
        self.assertIsNone(self.mia_slack["person_id"])

    def test_filters(self):
        get = lambda q: self.api("GET", f"/api/contacts{q}").get_json()
        self.assertEqual([c["id"] for c in get("?q=MIA")["items"]], [self.mia_slack["id"]])
        self.assertEqual(get("?q=%25")["total"], 0, "LIKE wildcards are literal")
        self.assertEqual(get(f"?channel_id={self.telegram['id']}")["total"], 1)

        person = self.mia()
        self.assertEqual(get(f"?person_id={person['id']}")["total"], 2)
        self.assertEqual([c["id"] for c in get("?unlinked=1")["items"]], [self.omar["id"]])
        self.assertEqual(get("?page_size=1&page=2")["items"][0]["display_name"], "Mia")

    def test_get_contact(self):
        resp = self.api("GET", f"/api/contacts/{self.omar['id']}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["display_name"], "Omar")
        self.assertEqual(self.api("GET", "/api/contacts/999").status_code, 404)

    def test_contacts_need_the_api_token(self):
        self.assertEqual(self.client.get("/api/contacts").status_code, 401)
        self.assertEqual(self.client.get("/api/people").status_code, 401)


class PersonCrudTests(PeopleTestCase):
    def test_create_get_list(self):
        person = self.mia()
        self.assertEqual(person["name"], "Mia")
        self.assertEqual(
            [(c["platform"], c["display_name"]) for c in person["contacts"]],
            [("slack", "Mia"), ("telegram", "Ana Silva")],
        )
        self.assertEqual(person["unread_count"], 2)

        fetched = self.api("GET", f"/api/people/{person['id']}").get_json()
        self.assertEqual(fetched, person)
        self.assertEqual(self.api("GET", f"/api/contacts/{self.mia_slack['id']}").get_json()["person_id"], person["id"])

        self.create_person("omar", [self.omar["id"]])
        self.create_person("Zed")
        listed = self.api("GET", "/api/people").get_json()
        self.assertEqual(listed["total"], 3)
        self.assertEqual([p["name"] for p in listed["items"]], ["Mia", "omar", "Zed"], "sorted by name, case-insensitively")
        self.assertEqual(listed["items"][2]["contacts"], [])
        self.assertEqual(listed["items"][2]["unread_count"], 0)
        self.assertEqual(len(self.api("GET", "/api/people?page_size=2&page=2").get_json()["items"]), 1)

    def test_create_without_contacts_and_validation(self):
        person = self.create_person("Nobody yet")
        self.assertEqual(person["contacts"], [])
        for body in ({}, {"name": "  "}, {"name": "x" * 101}, {"name": "ok", "contact_ids": []},
                     {"name": "ok", "contact_ids": "1"}, {"name": "ok", "contact_ids": [0]},
                     {"name": "ok", "contact_ids": [True]}, {"name": "ok", "contact_ids": ["1"]}):
            resp = self.api("POST", "/api/people", json=body)
            self.assertEqual(resp.status_code, 400, body)
        self.assertEqual(self.api("POST", "/api/people", data="nope").status_code, 400)

    def test_create_with_unknown_contact_creates_nothing(self):
        resp = self.api("POST", "/api/people", json={"name": "Mia", "contact_ids": [self.mia_slack["id"], 999]})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self.api("GET", "/api/people").get_json()["total"], 0)
        self.assertIsNone(self.api("GET", f"/api/contacts/{self.mia_slack['id']}").get_json()["person_id"])

    def test_rename(self):
        person = self.mia()
        resp = self.api("PATCH", f"/api/people/{person['id']}", json={"name": "Mia Park"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["name"], "Mia Park")
        self.assertEqual(len(resp.get_json()["contacts"]), 2)
        self.assertEqual(self.api("PATCH", f"/api/people/{person['id']}", json={"name": ""}).status_code, 400)
        self.assertEqual(self.api("PATCH", "/api/people/999", json={"name": "x"}).status_code, 404)

    def test_delete_ungroups_contacts(self):
        person = self.mia()
        self.assertEqual(self.api("DELETE", f"/api/people/{person['id']}").status_code, 204)
        self.assertEqual(self.api("GET", f"/api/people/{person['id']}").status_code, 404)
        self.assertEqual(self.api("DELETE", f"/api/people/{person['id']}").status_code, 404)
        # The contacts and their messages are untouched, just no longer grouped.
        self.assertIsNone(self.api("GET", f"/api/contacts/{self.mia_slack['id']}").get_json()["person_id"])
        self.assertEqual(self.api("GET", "/api/contacts?unlinked=1").get_json()["total"], 3)
        self.assertEqual(self.api("GET", "/api/messages?q=standup").get_json()["total"], 1)

    def test_deleting_a_channel_removes_its_contacts_from_the_person(self):
        person = self.mia()
        self.api("DELETE", f"/api/channels/{self.telegram['id']}")
        fetched = self.api("GET", f"/api/people/{person['id']}").get_json()
        self.assertEqual([c["platform"] for c in fetched["contacts"]], ["slack"])
        self.assertEqual(fetched["unread_count"], 1)


class LinkTests(PeopleTestCase):
    def test_link_and_unlink(self):
        person = self.create_person("Mia", [self.mia_slack["id"]])
        resp = self.api("POST", f"/api/people/{person['id']}/contacts", json={"contact_ids": [self.mia_telegram["id"]]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.get_json()["contacts"]), 2)
        self.assertEqual(resp.get_json()["unread_count"], 2)

        resp = self.api("DELETE", f"/api/people/{person['id']}/contacts/{self.mia_slack['id']}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([c["platform"] for c in resp.get_json()["contacts"]], ["telegram"])
        self.assertEqual(resp.get_json()["unread_count"], 1)
        self.assertIsNone(self.api("GET", f"/api/contacts/{self.mia_slack['id']}").get_json()["person_id"])

    def test_relinking_is_idempotent(self):
        person = self.mia()
        resp = self.api("POST", f"/api/people/{person['id']}/contacts",
                        json={"contact_ids": [self.mia_slack["id"], self.mia_slack["id"]]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.get_json()["contacts"]), 2)

    def test_contact_on_another_person_is_refused(self):
        mia = self.mia()
        resp = self.api("POST", "/api/people", json={"name": "Not Mia", "contact_ids": [self.mia_telegram["id"]]})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["error"]["code"], "contact_linked")
        self.assertEqual(self.api("GET", "/api/people").get_json()["total"], 1, "nothing was created")

        other = self.create_person("Other")
        resp = self.api("POST", f"/api/people/{other['id']}/contacts",
                        json={"contact_ids": [self.omar["id"], self.mia_slack["id"]]})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(self.api("GET", f"/api/people/{other['id']}").get_json()["contacts"], [],
                         "a refused batch links nothing")
        self.assertEqual(len(self.api("GET", f"/api/people/{mia['id']}").get_json()["contacts"]), 2)

        # Unlink first, then the move works.
        self.api("DELETE", f"/api/people/{mia['id']}/contacts/{self.mia_slack['id']}")
        resp = self.api("POST", f"/api/people/{other['id']}/contacts", json={"contact_ids": [self.mia_slack["id"]]})
        self.assertEqual(resp.status_code, 200)

    def test_link_errors(self):
        person = self.create_person("Mia")
        self.assertEqual(self.api("POST", f"/api/people/{person['id']}/contacts", json={"contact_ids": [999]}).status_code, 404)
        self.assertEqual(self.api("POST", f"/api/people/{person['id']}/contacts", json={"contact_ids": []}).status_code, 400)
        self.assertEqual(self.api("POST", f"/api/people/{person['id']}/contacts", json={}).status_code, 400)
        self.assertEqual(self.api("POST", "/api/people/999/contacts", json={"contact_ids": [1]}).status_code, 404)
        resp = self.api("DELETE", f"/api/people/{person['id']}/contacts/{self.omar['id']}")
        self.assertEqual(resp.status_code, 404, "not linked to this person")
        self.assertEqual(self.api("DELETE", f"/api/people/999/contacts/{self.omar['id']}").status_code, 404)


class UnreadCountTests(PeopleTestCase):
    def test_counts_only_unread_inbound_from_the_person(self):
        person = self.mia()
        self.assertEqual(person["unread_count"], 2)  # one per platform; Omar's does not count

        # Reading the Slack conversation clears Mia's Slack message (and Omar's).
        self.api("POST", f"/api/conversations/{self.conversation_id(self.slack)}/read")
        self.assertEqual(self.api("GET", f"/api/people/{person['id']}").get_json()["unread_count"], 1)

        # Our replies never count.
        self.api("POST", f"/api/conversations/{self.conversation_id(self.telegram)}/messages", json={"body": "sure"})
        self.assertEqual(self.api("GET", f"/api/people/{person['id']}").get_json()["unread_count"], 1)

        # A new message from either account counts; a deleted one stops counting.
        self.webhook(self.slack, slack_from("U-MIA", "Mia", "also: lunch?", T2))
        self.assertEqual(self.api("GET", f"/api/people/{person['id']}").get_json()["unread_count"], 2)
        self.webhook(self.slack, slack_deleted(deleted_ts=T2))
        self.assertEqual(self.api("GET", f"/api/people/{person['id']}").get_json()["unread_count"], 1)


class ConversationFilterTests(PeopleTestCase):
    def test_filter_conversations_by_person(self):
        person = self.mia()
        # A Slack DM from Omar only: must not show up for Mia.
        self.webhook(self.slack, slack_from("U-OMAR", "Omar", "psst", T2, channel="D-OMAR", channel_type="im"))
        self.assertEqual(self.api("GET", "/api/conversations").get_json()["total"], 3)

        result = self.api("GET", f"/api/conversations?person_id={person['id']}").get_json()
        self.assertEqual(result["total"], 2)
        self.assertEqual([c["platform"] for c in result["items"]], ["telegram", "slack"], "newest first")
        self.assertEqual(result["items"][1]["title"], "C1", "the shared channel counts: Mia wrote there")

    def test_combines_with_other_filters(self):
        person = self.mia()
        get = lambda q: self.api("GET", f"/api/conversations?person_id={person['id']}{q}").get_json()
        self.assertEqual(get(f"&channel_id={self.slack['id']}")["total"], 1)
        self.api("POST", f"/api/conversations/{self.conversation_id(self.slack)}/read")
        self.assertEqual([c["platform"] for c in get("&unread=1")["items"]], ["telegram"])

    def test_conversation_drops_out_when_the_persons_messages_are_deleted(self):
        person = self.mia()
        self.webhook(self.slack, slack_deleted(deleted_ts=T0))  # Mia's only Slack message
        result = self.api("GET", f"/api/conversations?person_id={person['id']}").get_json()
        self.assertEqual([c["platform"] for c in result["items"]], ["telegram"])

    def test_unknown_person_is_404(self):
        resp = self.api("GET", "/api/conversations?person_id=999")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self.api("GET", "/api/conversations?person_id=0").status_code, 400)


class TimelineTests(PeopleTestCase):
    def test_timeline_merges_platforms_newest_first(self):
        person = self.mia()
        self.webhook(self.telegram, telegram_message(message_id=2, chat_id=9002, user_id=9002, text="thanks!", date=1761500200))
        self.webhook(self.slack, slack_from("U-MIA", "Mia", "lunch?", "1761500150.000100"))
        self.api("POST", f"/api/conversations/{self.conversation_id(self.slack)}/messages", json={"body": "yes"})

        result = self.api("GET", f"/api/people/{person['id']}/timeline").get_json()
        self.assertEqual(result["person_id"], person["id"])
        self.assertEqual(result["total"], 4)
        self.assertEqual(
            [(m["conversation"]["platform"], m["body"]) for m in result["items"]],
            [("telegram", "thanks!"), ("slack", "lunch?"), ("telegram", "send me the notes?"),
             ("slack", "standup in 10?")],
        )
        self.assertEqual(result["items"][1]["conversation"]["title"], "C1")
        self.assertEqual(result["items"][1]["sender"]["display_name"], "Mia")
        self.assertEqual(result["items"][0]["sender"]["display_name"], "Ana Silva")
        bodies = {m["body"] for m in result["items"]}
        self.assertNotIn("running late", bodies, "Omar's message in the shared channel is not Mia's")
        self.assertNotIn("yes", bodies, "our own replies are not the person's messages")

    def test_timeline_pagination(self):
        person = self.mia()
        for i in range(3):
            self.webhook(self.telegram, telegram_message(message_id=10 + i, chat_id=9002, user_id=9002,
                                                         text=f"t{i}", date=1761500300 + i))
        page1 = self.api("GET", f"/api/people/{person['id']}/timeline?page_size=2").get_json()
        page3 = self.api("GET", f"/api/people/{person['id']}/timeline?page_size=2&page=3").get_json()
        self.assertEqual(page1["total"], 5)
        self.assertEqual([m["body"] for m in page1["items"]], ["t2", "t1"])
        self.assertEqual([m["body"] for m in page3["items"]], ["standup in 10?"])
        self.assertEqual(self.api("GET", f"/api/people/{person['id']}/timeline?page=0").status_code, 400)

    def test_timeline_hides_deleted_unless_asked(self):
        person = self.mia()
        self.webhook(self.slack, slack_deleted(deleted_ts=T0))
        visible = self.api("GET", f"/api/people/{person['id']}/timeline").get_json()
        self.assertEqual([m["body"] for m in visible["items"]], ["send me the notes?"])
        everything = self.api("GET", f"/api/people/{person['id']}/timeline?include_deleted=1").get_json()
        self.assertEqual(everything["total"], 2)
        self.assertIsNotNone(next(m for m in everything["items"] if m["body"] == "standup in 10?")["deleted_at"])

    def test_timeline_follows_links(self):
        person = self.create_person("Mia", [self.mia_slack["id"]])
        self.assertEqual(self.api("GET", f"/api/people/{person['id']}/timeline").get_json()["total"], 1)
        self.api("POST", f"/api/people/{person['id']}/contacts", json={"contact_ids": [self.mia_telegram["id"]]})
        self.assertEqual(self.api("GET", f"/api/people/{person['id']}/timeline").get_json()["total"], 2)
        self.api("DELETE", f"/api/people/{person['id']}/contacts/{self.mia_slack['id']}")
        self.assertEqual(self.api("GET", f"/api/people/{person['id']}/timeline").get_json()["total"], 1)

    def test_unknown_person_is_404(self):
        self.assertEqual(self.api("GET", "/api/people/999/timeline").status_code, 404)


if __name__ == "__main__":
    unittest.main()
