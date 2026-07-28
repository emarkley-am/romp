#!/usr/bin/env python3
"""moonlight plugin tests — the pure core: chip+columns → LED state, feed
folding, slots, paint diffs, WS codec, and the open-slot chord. The daemon's
IO edge (a live kernel, a real kontroll binary, a physical keyboard) is
exercised manually per the plugin README."""
import json
import os
import socket as so
import tempfile
import unittest
from importlib.machinery import SourceFileLoader

HERE = os.path.dirname(os.path.realpath(__file__))
ml = SourceFileLoader("moonlight", os.path.join(HERE, "moonlight.py")).load_module()


def chip(state, **kw):
    d = {"state": state, "faded": False}
    d.update(kw)
    return d


class LedState(unittest.TestCase):
    def test_precedence_table(self):
        cases = [
            (chip("awaiting"), set(), "needs_you", 1.0),
            (chip("working"), {"needs_input"}, "needs_you", 1.0),
            (chip("blocked"), set(), "api_error", 1.0),
            (chip("compacting"), set(), "compacting", 1.0),
            (chip("interrupting"), set(), "compacting", 1.0),
            (chip("working"), set(), "working", 1.0),
            (chip("retrying"), set(), "working", 1.0),
            (chip("ready"), {"working"}, "working", 1.0),
            (chip("awaitingBg"), set(), "working", 0.35),
            (chip("ready"), {"completed"}, "completed", 1.0),
            (chip("working"), {"completed"}, "working", 1.0),
            (chip("ready"), set(), "idle", 0.4),
            (chip("ready", faded=True), set(), "idle", 0.15),
            (chip(""), set(), "off", 1.0),
            (None, set(), "off", 1.0),
        ]
        for status, cols, want_state, want_b in cases:
            self.assertEqual(ml.led_state(status, cols), (want_state, want_b),
                             "chip=%r cols=%r" % (status, cols))

    def test_worst(self):
        self.assertEqual(ml.worst(["completed", "working", "needs_you"]), "needs_you")
        self.assertEqual(ml.worst(["idle", "completed"]), "completed")
        self.assertEqual(ml.worst([]), "off")

    def test_dim(self):
        self.assertEqual(ml.dim("#e0b020", 1.0), "#e0b020")
        self.assertEqual(ml.dim("#ff0000", 0.5), "#7f0000")
        self.assertEqual(ml.dim("#ffffff", 0.0), "#000000")


class ParseFeed(unittest.TestCase):
    def test_folds_ledgers_and_asks(self):
        payload = {"type": "feed",
                   "ledgers": [{"sid": "11111111-1111", "name": "web", "color": "#9cd2ff",
                                "status": chip("working")},
                               {"sid": "22222222-2222", "name": "api", "color": None,
                                "status": chip("ready")}],
                   "asks": [{"sid": "11111111-1111", "column": "working"},
                            {"sid": "22222222-2222", "column": "completed"},
                            {"sid": "99999999-9999", "column": "needs_input"}]}
        out = ml.parse_feed(payload)
        self.assertEqual(set(out), {"11111111-1111", "22222222-2222"})
        self.assertEqual(out["11111111-1111"]["columns"], {"working"})
        self.assertEqual(out["22222222-2222"]["columns"], {"completed"})
        self.assertEqual(out["22222222-2222"]["name"], "api")


class Slots(unittest.TestCase):
    def sess(self, *sids):
        return {s: {"status": chip("working"), "columns": set(), "name": "s" + s[:1], "color": None}
                for s in sids}

    def test_sticky_and_fill(self):
        keys = [10, 11, 12]
        slots, over = ml.assign_slots({}, ["a", "b"], {}, keys)
        self.assertEqual(slots, {"a": 10, "b": 11})
        self.assertEqual(over, [])
        slots2, _ = ml.assign_slots(slots, ["a", "c"], {}, keys)
        self.assertEqual(slots2["a"], 10)
        self.assertIn(slots2["c"], (11, 12))

    def test_pins_win(self):
        slots, _ = ml.assign_slots({"a": 12}, ["a", "b"], {"b": 12}, [10, 11, 12])
        self.assertEqual(slots["b"], 12)
        self.assertNotEqual(slots["a"], 12)

    def test_overflow_reserves_last_key(self):
        slots, over = ml.assign_slots({}, ["a", "b", "c", "d"], {}, [10, 11, 12])
        self.assertEqual(len(slots), 2)
        self.assertEqual(set(slots.values()), {10, 11})
        self.assertEqual(over, ["c", "d"])

    def test_ordered_sids_follows_feed_order(self):
        sessions = {"x": {"name": "web"}, "y": {"name": "api"}, "z": {"name": "tests"}}
        self.assertEqual(ml.ordered_sids(sessions, ["api", "web"]), ["y", "x", "z"])

    def test_write_slots(self):
        sessions = self.sess("a", "b")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "slots.json")
            ml.write_slots({"a": 11, "b": 10}, sessions, [10, 11], p)
            data = json.loads(open(p).read())
            self.assertEqual(data["slots"][0], {"slot": 0, "led": 10, "sid": "b", "name": "sb"})
            self.assertEqual(data["slots"][1]["sid"], "a")


class FakeKontroll:
    def __init__(self):
        self.calls = []

    def set_rgb(self, led, color):
        self.calls.append((led, color))

    def set_rgb_all(self, color):
        self.calls.append(("all", color))

    def restore(self):
        self.calls.append(("restore",))


class Paint(unittest.TestCase):
    def test_build_paint_and_pulse(self):
        sessions = {"a": {"status": chip("awaiting"), "columns": set(), "name": "web", "color": None},
                    "b": {"status": chip("ready"), "columns": set(), "name": "api", "color": "#9cd2ff"}}
        paint, pulse = ml.build_paint(sessions, {"a": 10, "b": 11}, [], [10, 11, 12], "dim")
        self.assertEqual(paint[10], ml.COLORS["needs_you"])
        self.assertEqual(paint[11], ml.dim(ml.COLORS["idle"], 0.4))
        self.assertEqual(paint[12], ml.COLORS["off"])
        self.assertEqual(pulse, {10})

    def test_idle_identity_mode(self):
        sessions = {"b": {"status": chip("ready"), "columns": set(), "name": "api", "color": "#9cd2ff"}}
        paint, _ = ml.build_paint(sessions, {"b": 11}, [], [11], "identity")
        self.assertEqual(paint[11], ml.dim("#9cd2ff", 0.4))

    def test_overflow_lamp_is_worst(self):
        sessions = {"a": {"status": chip("working"), "columns": set(), "name": "w", "color": None},
                    "c": {"status": chip("awaiting"), "columns": set(), "name": "x", "color": None},
                    "d": {"status": chip("ready"), "columns": {"completed"}, "name": "y", "color": None}}
        paint, pulse = ml.build_paint(sessions, {"a": 10}, ["c", "d"], [10, 11], "dim")
        self.assertEqual(paint[11], ml.COLORS["needs_you"])
        self.assertIn(11, pulse)

    def test_pulsed_dims_only_pulse_leds(self):
        p = ml.pulsed({10: "#c0392b", 11: "#e0b020"}, {10}, True)
        self.assertEqual(p[10], ml.dim("#c0392b", 0.2))
        self.assertEqual(p[11], "#e0b020")
        p2 = ml.pulsed({10: "#c0392b"}, {10}, False)
        self.assertEqual(p2[10], "#c0392b")

    def test_apply_paint_diffs_only(self):
        f = FakeKontroll()
        prev = ml.apply_paint(f, {}, {10: "#111111", 11: "#222222"})
        self.assertEqual(len(f.calls), 2)
        f.calls.clear()
        ml.apply_paint(f, prev, {10: "#111111", 11: "#333333"})
        self.assertEqual(f.calls, [(11, "#333333")])


class KontrollArgs(unittest.TestCase):
    def test_arg_templates(self):
        runs = []
        k = ml.Kontroll({"kontroll": "kontroll-test",
                         "set_rgb_args": "set-rgb {led} --color {color}",
                         "set_rgb_all_args": "set-rgb-all --color {color}"}, run=lambda a: runs.append(a))
        k.set_rgb(33, "#e0b020")
        k.set_rgb_all("#c0392b")
        k.restore()
        self.assertEqual(runs, [["kontroll-test", "set-rgb", "33", "--color", "#e0b020"],
                                ["kontroll-test", "set-rgb-all", "--color", "#c0392b"],
                                ["kontroll-test", "restore-rgb-leds"]])


class Step(unittest.TestCase):
    CFG = {"keys": [10, 11], "idle": "dim"}

    def payload(self, state_a="working", cols_a=()):
        return {"type": "feed", "order": ["web"],
                "ledgers": [{"sid": "a", "name": "web", "color": None, "status": chip(state_a)}],
                "asks": [{"sid": "a", "column": c} for c in cols_a]}

    def test_step_paints_and_flags_flash(self):
        st = ml.step({}, self.payload("working"), self.CFG)
        self.assertEqual(st["paint"][10], ml.COLORS["working"])
        self.assertFalse(st["flash"])
        st2 = ml.step(st, self.payload("awaiting"), self.CFG)
        self.assertTrue(st2["flash"])
        st3 = ml.step(st2, self.payload("awaiting"), self.CFG)
        self.assertFalse(st3["flash"])

    def test_slots_survive_across_steps(self):
        st = ml.step({}, self.payload(), self.CFG)
        two = {"type": "feed", "order": ["api", "web"],
               "ledgers": [{"sid": "a", "name": "web", "color": None, "status": chip("working")},
                           {"sid": "b", "name": "api", "color": None, "status": chip("ready")}],
               "asks": []}
        st2 = ml.step(st, two, self.CFG)
        self.assertEqual(st2["slots"]["a"], 10)


class Config(unittest.TestCase):
    def test_missing_config_exits_loudly(self):
        with tempfile.TemporaryDirectory() as d:
            old = ml.CONFIG
            ml.CONFIG = ml.Path(d) / "keyboard.json"
            try:
                with self.assertRaises(SystemExit):
                    ml.load_config()
                ml.CONFIG.write_text('{"keys": []}')
                with self.assertRaises(SystemExit):
                    ml.load_config()
                ml.CONFIG.write_text('{"keys": [1, 2]}')
                self.assertEqual(ml.load_config()["keys"], [1, 2])
            finally:
                ml.CONFIG = old


def _frame(payload, op=0x1):
    b = payload.encode() if isinstance(payload, str) else payload
    n = len(b)
    if n < 126:
        head = bytes([0x80 | op, n])
    elif n < 65536:
        head = bytes([0x80 | op, 126]) + n.to_bytes(2, "big")
    else:
        head = bytes([0x80 | op, 127]) + n.to_bytes(8, "big")
    return head + b


class WSCodec(unittest.TestCase):
    def pair(self):
        a, b = so.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        return ml.WS(a), b

    def test_text_roundtrip_and_lengths(self):
        ws, peer = self.pair()
        peer.sendall(_frame("hello") + _frame("x" * 300))
        self.assertEqual(ws.recv_text(timeout=1), "hello")
        self.assertEqual(ws.recv_text(timeout=1), "x" * 300)

    def test_ping_answered_then_text(self):
        ws, peer = self.pair()
        peer.sendall(_frame(b"p", op=0x9) + _frame("after"))
        self.assertEqual(ws.recv_text(timeout=1), "after")
        pong = peer.recv(64)
        self.assertEqual(pong[0] & 0x0F, 0xA)
        self.assertTrue(pong[1] & 0x80)

    def test_timeout_returns_none(self):
        ws, _peer = self.pair()
        self.assertIsNone(ws.recv_text(timeout=0.05))

    def test_close_raises(self):
        ws, peer = self.pair()
        peer.sendall(_frame(b"", op=0x8))
        with self.assertRaises(ConnectionError):
            ws.recv_text(timeout=1)

    def test_send_text_is_masked(self):
        ws, peer = self.pair()
        ws.send_text('{"type":"openByName","name":"web"}')
        raw = peer.recv(256)
        self.assertEqual(raw[0], 0x81)
        self.assertTrue(raw[1] & 0x80)
        n = raw[1] & 0x7F
        mask, data = raw[2:6], raw[6:6 + n]
        text = bytes(c ^ mask[i % 4] for i, c in enumerate(data)).decode()
        self.assertIn("openByName", text)


class OpenSlot(unittest.TestCase):
    def test_open_slot_sends_and_switches(self):
        opens, tmuxes = [], []
        with tempfile.TemporaryDirectory() as d:
            old = ml.SLOTS_FILE
            ml.SLOTS_FILE = ml.Path(d) / "slots.json"
            try:
                ml.SLOTS_FILE.write_text(json.dumps(
                    {"port": 29855, "slots": [{"slot": 0, "led": 10, "sid": "11111111", "name": "web"}]}))
                rc = ml.open_slot(0, ws_open=lambda name: opens.append(name),
                                  tmux_run=lambda name: tmuxes.append(name))
                self.assertEqual(rc, 0)
                self.assertEqual(opens, ["web"])
                self.assertEqual(tmuxes, ["web"])
                rc = ml.open_slot(3, ws_open=lambda n: None, tmux_run=lambda n: None)
                self.assertEqual(rc, 1)
            finally:
                ml.SLOTS_FILE = old

    def test_skhd_lines(self):
        lines = ml.skhd_lines([33, 34, 35])
        self.assertEqual(lines[0], "f13 : moonlight open-slot 0")
        self.assertEqual(lines[2], "f15 : moonlight open-slot 2")


if __name__ == "__main__":
    unittest.main()
