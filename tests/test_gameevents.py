"""Source replay, connection ownership, retention and pressure are observable contracts."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid

from gameevents import GameEvents, Source, SourceDenied, EventConflict, Backpressure


class GameEventTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "private/events.sqlite3"
        self.now = 1000
        self.journal = self.open()
        self.source = self.journal.open_source()
        self.player = "a" * 32
        self.context = "b" * 32
        self.started = self.event(1, "server.started")
        self.journal.append(self.source, self.started)

    def open(self, **options):
        return GameEvents(self.path, chat=True, clock=lambda: self.now, **options)

    def event(self, sequence, kind, **data):
        return {"schema": 1, "boot": self.source.boot, "id": uuid.uuid4().hex,
                "sequence": sequence, "kind": kind, "occurred": self.now, "data": data}

    def join(self, sequence=2, context=None):
        event = self.event(sequence, "player.joined", uuid=self.player, name="Alice", connection=context or self.context)
        self.journal.append(self.source, event)
        return event

    def chat(self, sequence=3, **changes):
        return self.event(sequence, "player.chat", **({"uuid": self.player, "connection": self.context,
                     "text": "original", "signature_sha256": "c" * 64} | changes))

    def test_source_token_is_private_and_scoped_to_one_boot(self):
        other = self.journal.open_source()
        for source in (Source(other.boot, self.source.token), Source(self.source.boot, other.token),
                       Source(self.source.boot, "wrong"), Source([], self.source.token)):
            with self.subTest(source=source), self.assertRaises(SourceDenied):
                self.journal.source_state(source)
        self.assertNotIn(self.source.token, repr(self.source))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertNotIn(self.source.token, json.dumps(list(db.execute("SELECT * FROM game_sources"))))
        self.assertNotIn(self.source.token, json.dumps(self.journal.poll()))

    def test_poll_tail_is_atomic_and_independent_of_page_or_hidden_chat(self):
        self.join()
        self.journal.append(self.source, self.chat())
        page = self.journal.poll(limit=1)
        self.assertEqual((page['floor'], page['cursor'], page['tail'], page['more']), (0, 1, 3, True))
        hidden = self.journal.poll(2, epoch=page['epoch'], limit=1)
        self.assertEqual(hidden['events'], [])
        self.assertEqual((hidden['cursor'], hidden['tail'], hidden['more']), (3, 3, False))
        visible = self.journal.poll(2, epoch=page['epoch'], limit=1, chat=True)
        self.assertEqual(visible['events'][0]['kind'], 'player.chat')
        self.assertEqual((visible['cursor'], visible['tail']), (3, 3))

    def test_receipt_replay_survives_restart_without_reapplying_old_join(self):
        first = self.join()
        receipt = self.journal.append(self.source, first)
        self.assertTrue(receipt["duplicate"])
        self.join(3, "d" * 32)
        self.journal = self.open()
        replay = self.journal.append(self.source, first)
        self.assertEqual(replay["cursor"], receipt["cursor"])
        self.assertEqual(self.journal.roster(self.source)["players"][0]["connection"], "d" * 32)
        self.assertEqual(len(self.journal.poll()["events"]), 3)

    def test_conflicting_replay_sequence_gap_and_event_id_reuse_refuse_mutation(self):
        first = self.join()
        invalid = [{**first, "data": {**first["data"], "name": "Mallory"}},
                   self.event(4, "player.left", uuid=self.player, connection=self.context),
                   {**self.chat(), "id": first["id"]}]
        for event in invalid:
            with self.subTest(event=event), self.assertRaises(EventConflict):
                self.journal.append(self.source, event)
        self.assertEqual(self.journal.source_state(self.source)["sequence"], 2)
        self.assertEqual(self.journal.roster(self.source)["players"][0]["name"], "Alice")

    def test_delayed_disconnect_cannot_remove_new_connection_and_chat_requires_current_context(self):
        self.join()
        self.join(3, "d" * 32)
        self.journal.append(self.source, self.event(4, "player.left", uuid=self.player, connection=self.context))
        with self.assertRaises(EventConflict):
            self.journal.append(self.source, self.chat(5))
        accepted = self.chat(5, connection="d" * 32)
        self.journal.append(self.source, accepted)
        data = self.journal.poll(chat=True)["events"][-1]["data"]
        self.assertEqual(data["name"], "Alice")
        self.assertEqual(data["signature_sha256"], "c" * 64)
        self.assertEqual(self.journal.roster(self.source)["players"][0]["connection"], "d" * 32)

    def test_forged_name_in_chat_and_messages_before_join_are_rejected(self):
        with self.assertRaises(EventConflict):
            self.journal.append(self.source, self.chat(2))
        self.join()
        event = self.chat()
        event["data"]["name"] = "Admin"
        with self.assertRaises(ValueError):
            self.journal.append(self.source, event)
        self.assertEqual(self.journal.source_state(self.source)["sequence"], 2)

    def test_chat_is_opt_in_and_readers_without_chat_access_advance_past_hidden_rows(self):
        self.join()
        self.journal.append(self.source, self.chat())
        hidden = self.journal.poll()
        self.assertNotIn("original", json.dumps(hidden))
        self.assertEqual(hidden["cursor"], 3)
        self.assertEqual(len(hidden["events"]), 2)
        self.assertEqual(self.journal.poll(chat=True)["events"][-1]["data"]["text"], "original")
        disabled = GameEvents(self.path, clock=lambda: self.now)
        with self.assertRaises(SourceDenied):
            disabled.append(self.source, self.chat(4))
        with self.assertRaises(SourceDenied):
            disabled.poll(chat=True)
        self.assertNotIn("original", json.dumps(disabled.poll()))

    def test_pressure_rolls_back_roster_and_sequence_so_exact_retry_can_succeed(self):
        self.journal = self.open(max_events=1)
        joining = self.event(2, "player.joined", uuid=self.player, name="Alice", connection=self.context)
        with self.assertRaises(Backpressure):
            self.journal.append(self.source, joining)
        self.assertEqual(self.journal.roster(self.source)["players"], [])
        self.assertEqual(self.journal.source_state(self.source)["sequence"], 1)
        self.journal = self.open(max_events=3)
        self.assertFalse(self.journal.append(self.source, joining)["duplicate"])
        self.assertEqual(self.journal.source_state(self.source)["sequence"], 2)

    def test_retention_reset_is_explicit_and_receipts_do_not_resurrect_expired_content(self):
        self.join()
        self.journal.append(self.source, self.chat())
        initial = self.journal.poll(chat=True)
        self.now = 1301
        current = self.journal.poll(0, epoch=initial["epoch"], chat=True)
        self.assertTrue(current["reset"])
        self.assertEqual(current["floor"], 3)
        self.assertEqual(current["events"], [])
        self.assertNotIn("original", json.dumps(current))
        with self.assertRaises(EventConflict):
            self.journal.append(self.source, self.started)
        self.assertEqual(self.journal.source_state(self.source)["sequence"], 3)

    def test_retired_boot_accepts_previously_captured_history_without_restoring_presence(self):
        self.join()
        captured = self.chat()
        self.now += 2
        self.journal.retire_source(self.source)
        self.assertEqual(self.journal.roster(self.source)["players"], [])
        self.assertFalse(self.journal.append(self.source, captured)["duplicate"])
        self.assertEqual(self.journal.roster(self.source)["players"], [])
        self.now += 1
        with self.assertRaises(EventConflict):
            self.journal.append(self.source, self.chat(4))
        self.assertTrue(self.journal.source_state(self.source)["retired"])

    def test_lifecycle_starts_once_and_stopping_refuses_new_chat(self):
        with self.assertRaises(EventConflict):
            self.journal.append(self.source, self.event(2, "server.started"))
        self.join()
        self.journal.append(self.source, self.event(3, "server.stopping"))
        with self.assertRaises(EventConflict):
            self.journal.append(self.source, self.chat(4))
        self.journal.append(self.source, self.event(4, "player.left", uuid=self.player, connection=self.context))
        self.assertEqual(self.journal.roster(self.source)["players"], [])

    def test_clock_reversal_does_not_make_retention_skip_later_rows(self):
        self.now = 1002
        self.join()
        self.now = 1001
        fresh = self.journal.open_source("second")
        event = {**self.event(1, "server.started"), "boot": fresh.boot}
        self.journal.append(fresh, event)
        self.now = 1301
        page = self.journal.poll()
        self.assertEqual(page["floor"], 1)
        self.assertEqual([event["cursor"] for event in page["events"]], [2, 3])

    def test_pages_have_bounded_scan_and_cannot_request_future_cursor(self):
        self.join()
        self.journal.append(self.source, self.chat())
        page = self.journal.poll(limit=1)
        self.assertTrue(page["more"])
        next_page = self.journal.poll(page["cursor"], epoch=page["epoch"], limit=1)
        self.assertFalse(next_page["reset"])
        self.assertEqual(next_page["events"][0]["kind"], "player.joined")
        with self.assertRaises(ValueError):
            self.journal.poll(999, epoch=page["epoch"])
        for limit in (True, 0, 101):
            with self.assertRaises(ValueError):
                self.journal.poll(limit=limit)


if __name__ == "__main__":
    unittest.main()
