#!/usr/bin/env python3
"""moonlight — mirror romp session status onto a ZSA keyboard's RGB keys.

Each configured LED is one session's status lamp: yellow working, pulsing red
needs-you, steady red API error, teal compacting, green completed, dim blue
idle. A leader-key chord (press leader, then a lit key) jumps to that session.

Status comes from the romp kernel's WS push (/ws?app=fleet — the feed payload:
ledgers = the chip, asks = the goal columns), never re-derived from state files.
LEDs are driven through ZSA's `kontroll` CLI (the Keymapp API — Keymapp must be
running with its API enabled). Kernel unreachable → every mapped key turns dim
white: a loud, visible error, never a silently stale board.

This is a romp PLUGIN: it uses only the kernel's public WS/HTTP API (see
plugins/README.md) and imports nothing from romp's code. It runs as a
standalone daemon alongside romp.

Config (~/.config/romp/keyboard.json):
  {"keys": [33, 34, 35],          # LED indices, slot order (--identify to find them)
   "pins": {"web": 33},           # optional: session name -> LED
   "idle": "dim",                 # "dim" | "off" | "identity" (session's romp color)
   "kontroll": "kontroll",        # binary path
   "set_rgb_args": "set-rgb {led} --color {color}",     # arg templates, so a kontroll
   "set_rgb_all_args": "set-rgb-all --color {color}"}   # syntax drift is a config fix

Usage:
  moonlight                    # daemon (foreground; ctrl-c restores the LEDs)
  moonlight --ensure           # spawn the detached daemon if not already running
  moonlight --once             # one paint from MOONLIGHT_FEED_FILE (tests / manual)
  moonlight --identify         # light each configured LED in turn, printing its index
  moonlight --skhd             # print the skhd bindings for the leader-layer keys
  moonlight open-slot N        # focus the session on slot N (the leader chord's target)
"""
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from base64 import b64encode
from pathlib import Path

HOME    = Path.home()
STATE   = Path(os.environ.get("ROMP_STATE_DIR")
               or Path(os.environ.get("XDG_STATE_HOME", str(HOME / ".local/state"))) / "romp")
CONFIG  = Path(os.environ.get("MOONLIGHT_CONFIG")
               or Path(os.environ.get("XDG_CONFIG_HOME", str(HOME / ".config"))) / "romp" / "keyboard.json")
PIDFILE = STATE / "moonlight.pid"
SLOTS_FILE = STATE / "moonlight-slots.json"
PORT    = int(os.environ.get("ROMP_SERVE_PORT", "29855"))

PULSE_SECS = 0.7
FLASH_SECS = 0.3

COLORS = {
    "needs_you":  "#c0392b",
    "api_error":  "#e5484d",
    "working":    "#e0b020",
    "compacting": "#14b8a6",
    "completed":  "#2ecc71",
    "idle":       "#2b7fb8",
    "error":      "#444444",
    "off":        "#000000",
}
PRECEDENCE = ("needs_you", "api_error", "working", "compacting", "completed", "idle", "off")


# ── pure core (unit-tested) ─────────────────────────────────────────

def led_state(status, columns):
    """(state, brightness) for one session. needs-you means a HUMAN is the
    bottleneck — a live prompt or a blocked card; awaitingBg (waiting on
    dispatched bg work) is deliberately dimmed working, never red."""
    st = (status or {}).get("state") or ""
    if st == "awaiting" or "needs_input" in columns:
        return "needs_you", 1.0
    if st == "blocked":
        return "api_error", 1.0
    if st in ("compacting", "interrupting"):
        return "compacting", 1.0
    if st in ("working", "retrying") or "working" in columns:
        return "working", 1.0
    if st == "awaitingBg":
        return "working", 0.35
    if "completed" in columns:
        return "completed", 1.0
    if st == "ready":
        return "idle", 0.15 if (status or {}).get("faded") else 0.4
    return "off", 1.0


def worst(states):
    order = {s: i for i, s in enumerate(PRECEDENCE)}
    return min(states, key=lambda s: order.get(s, len(order)), default="off")


def dim(hexcolor, f):
    r, g, b = (int(hexcolor[i:i + 2], 16) for i in (1, 3, 5))
    return "#%02x%02x%02x" % (int(r * f), int(g * f), int(b * f))


def parse_feed(payload):
    """Fold one kernel feed push into {sid: {status, columns, name, color}}."""
    out = {}
    for led in payload.get("ledgers") or []:
        out[led["sid"]] = {"status": led.get("status") or {}, "columns": set(),
                           "name": led.get("name") or led["sid"], "color": led.get("color")}
    for card in payload.get("asks") or []:
        s = out.get(card.get("sid"))
        if s is not None and card.get("column"):
            s["columns"].add(card["column"])
    return out


def ordered_sids(sessions, order):
    byname = {v["name"]: k for k, v in sessions.items()}
    out = [byname[n] for n in (order or []) if n in byname]
    out += sorted(set(sessions) - set(out), key=lambda s: sessions[s]["name"])
    return out


def resolve_pins(pins_by_name, sessions):
    byname = {v["name"]: k for k, v in sessions.items()}
    return {byname[n]: led for n, led in (pins_by_name or {}).items() if n in byname}


def assign_slots(prev, sids, pinned, keys):
    """sid→LED. Pins first, then sticky, then free. When sessions outnumber
    LEDs the last key is held back as the overflow lamp."""
    keys = list(keys)
    usable = keys[:-1] if len(sids) > len(keys) and keys else keys
    slots = {}
    for sid in sids:
        led = pinned.get(sid)
        if led in usable and led not in slots.values():
            slots[sid] = led
    for sid in sids:
        if sid in slots:
            continue
        led = prev.get(sid)
        if led in usable and led not in slots.values():
            slots[sid] = led
    free = [k for k in usable if k not in slots.values()]
    overflow = []
    for sid in sids:
        if sid in slots:
            continue
        if free:
            slots[sid] = free.pop(0)
        else:
            overflow.append(sid)
    return slots, overflow


def write_slots(slots, sessions, keys, path=None):
    pos = {led: i for i, led in enumerate(keys)}
    rows = sorted(({"slot": pos[led], "led": led, "sid": sid, "name": sessions[sid]["name"]}
                   for sid, led in slots.items() if led in pos), key=lambda r: r["slot"])
    p = Path(path or SLOTS_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"port": PORT, "slots": rows}))
    tmp.replace(p)


def build_paint(sessions, slots, overflow, keys, idle_mode="dim"):
    paint = {k: COLORS["off"] for k in keys}
    pulse = set()
    for sid, led in slots.items():
        st, bright = led_state(sessions[sid]["status"], sessions[sid]["columns"])
        color = COLORS[st]
        if st == "idle":
            if idle_mode == "off":
                color = COLORS["off"]
            elif idle_mode == "identity" and sessions[sid].get("color"):
                color = sessions[sid]["color"]
        paint[led] = dim(color, bright) if bright != 1.0 else color
        if st == "needs_you":
            pulse.add(led)
    if overflow and keys:
        st = worst([led_state(sessions[s]["status"], sessions[s]["columns"])[0] for s in overflow])
        paint[keys[-1]] = COLORS[st]
        if st == "needs_you":
            pulse.add(keys[-1])
    return paint, pulse


def pulsed(paint, pulse_leds, phase):
    if not phase or not pulse_leds:
        return dict(paint)
    return {led: (dim(c, 0.2) if led in pulse_leds else c) for led, c in paint.items()}


def apply_paint(board, prev, new):
    for led, color in new.items():
        if prev.get(led) != color:
            board.set_rgb(led, color)
    return new


def step(state, payload, cfg):
    """Fold one feed push into the next daemon state — pure."""
    sessions = parse_feed(payload)
    sids = ordered_sids(sessions, payload.get("order"))
    slots, overflow = assign_slots(state.get("slots") or {}, sids,
                                   resolve_pins(cfg.get("pins"), sessions), cfg["keys"])
    paint, pulse = build_paint(sessions, slots, overflow, cfg["keys"], cfg.get("idle", "dim"))
    needs = {sid for sid in list(slots) + overflow
             if led_state(sessions[sid]["status"], sessions[sid]["columns"])[0] == "needs_you"}
    return {"sessions": sessions, "slots": slots, "paint": paint, "pulse": pulse,
            "needs": needs, "flash": bool(needs - (state.get("needs") or set()))}


# ── kontroll driver ─────────────────────────────────────────────────

class Kontroll:
    def __init__(self, cfg, run=None):
        self.bin = cfg.get("kontroll", "kontroll")
        self.rgb_t = cfg.get("set_rgb_args", "set-rgb {led} --color {color}")
        self.all_t = cfg.get("set_rgb_all_args", "set-rgb-all --color {color}")
        self._run = run or self._subprocess_run

    def _subprocess_run(self, args):
        try:
            subprocess.run(args, capture_output=True, timeout=5)
        except Exception:
            pass

    def set_rgb(self, led, color):
        self._run([self.bin] + [a.format(led=led, color=color) for a in self.rgb_t.split()])

    def set_rgb_all(self, color):
        self._run([self.bin] + [a.format(color=color) for a in self.all_t.split()])

    def restore(self):
        self._run([self.bin, "restore-rgb-leds"])


# ── minimal WS client ──────────────────────────────────────────────

def _read_token():
    return (STATE / "serve-token").read_text().strip()


def ws_connect(host, port, token, app="fleet"):
    s = socket.create_connection((host, port), timeout=5)
    key = b64encode(os.urandom(16)).decode()
    s.sendall(("GET /ws?app=%s&wid=moonlight&token=%s HTTP/1.1\r\n"
               "Host: %s:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n"
               % (app, token, host, port, key)).encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = s.recv(4096)
        if not chunk:
            raise ConnectionError("kernel closed mid-handshake")
        resp += chunk
    status = resp.split(b"\r\n", 1)[0]
    if b" 101 " not in status:
        raise ConnectionError("WS upgrade refused: %s" % status.decode("ascii", "replace"))
    return s


class WS:
    def __init__(self, sock):
        self.s = sock
        self.buf = b""

    def _read(self, n, timeout):
        while len(self.buf) < n:
            self.s.settimeout(timeout)
            chunk = self.s.recv(65536)
            if not chunk:
                raise ConnectionError("socket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv_text(self, timeout=None):
        while True:
            try:
                h = self._read(2, timeout)
            except (socket.timeout, TimeoutError):
                return None
            op, n = h[0] & 0x0F, h[1] & 0x7F
            if n == 126:
                n = int.from_bytes(self._read(2, None), "big")
            elif n == 127:
                n = int.from_bytes(self._read(8, None), "big")
            payload = self._read(n, None)
            if op == 0x9:
                self._send_frame(payload, 0xA)
                continue
            if op == 0x8:
                raise ConnectionError("server closed WS")
            if op == 0x1:
                return payload.decode("utf-8", "replace")

    def send_text(self, text):
        self._send_frame(text.encode(), 0x1)

    def _send_frame(self, data, op):
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            head = bytes([0x80 | op, 0x80 | n])
        elif n < 65536:
            head = bytes([0x80 | op, 0x80 | 126]) + n.to_bytes(2, "big")
        else:
            head = bytes([0x80 | op, 0x80 | 127]) + n.to_bytes(8, "big")
        self.s.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))


# ── focus (the leader chord's landing) ──────────────────────────────

def _ws_open_by_name(name):
    """Open a brief WS to the kernel and send openByName — the same verb the
    dashboard uses to focus a session. Fully self-contained: no POST route
    needed, just the existing WS API."""
    token = _read_token()
    sock = ws_connect("127.0.0.1", PORT, token, app="chat")
    ws = WS(sock)
    ws.send_text(json.dumps({"type": "openByName", "name": name}))
    sock.close()


def _tmux_switch(name):
    try:
        out = subprocess.run(["tmux", "list-clients", "-F", "#{client_tty}"],
                             capture_output=True, text=True, timeout=5).stdout.split()
        if out:
            subprocess.run(["tmux", "switch-client", "-c", out[-1], "-t", name],
                           capture_output=True, timeout=5)
    except Exception:
        pass


def open_slot(n, ws_open=None, tmux_run=None):
    """Slot N → the session it shows → WS openByName + tmux jump."""
    try:
        rows = json.loads(SLOTS_FILE.read_text()).get("slots") or []
    except Exception:
        sys.stderr.write("moonlight: no slot map at %s — is the daemon running?\n" % SLOTS_FILE)
        return 1
    row = next((r for r in rows if r.get("slot") == n), None)
    if not row:
        sys.stderr.write("moonlight: nothing on slot %d\n" % n)
        return 1
    try:
        (ws_open or _ws_open_by_name)(row["name"])
    except Exception as e:
        sys.stderr.write("moonlight: kernel not reachable: %s\n" % e)
        return 1
    (tmux_run or _tmux_switch)(row["name"])
    return 0


def skhd_lines(keys):
    return ["f%d : moonlight open-slot %d" % (13 + i, i) for i in range(len(keys))]


def print_skhd():
    cfg = load_config()
    print("# moonlight leader-layer bindings — append to ~/.config/skhd/skhdrc")
    for line in skhd_lines(cfg["keys"]):
        print(line)


# ── config + lifecycle ──────────────────────────────────────────────

def load_config():
    if not CONFIG.exists():
        sys.stderr.write("moonlight: no config at %s — see the README\n" % CONFIG)
        sys.exit(2)
    try:
        cfg = json.loads(CONFIG.read_text())
        keys = cfg.get("keys")
        assert keys and all(isinstance(k, int) for k in keys)
    except SystemExit:
        raise
    except Exception:
        sys.stderr.write("moonlight: %s is invalid — needs a non-empty integer 'keys' list\n" % CONFIG)
        sys.exit(2)
    return cfg


def run(cfg, board, stop):
    token = _read_token()
    st, prev, phase, backoff = {}, {}, False, 1
    err_paint = {k: COLORS["error"] for k in cfg["keys"]}
    try:
        while not stop.is_set():
            try:
                ws = WS(ws_connect("127.0.0.1", PORT, token))
                backoff = 1
                while not stop.is_set():
                    txt = ws.recv_text(timeout=PULSE_SECS)
                    if txt is None:
                        phase = not phase
                        if st.get("pulse"):
                            prev = apply_paint(board, prev, pulsed(st["paint"], st["pulse"], phase))
                        continue
                    msg = json.loads(txt)
                    if msg.get("type") != "feed":
                        continue
                    st = step(st, msg, cfg)
                    if st["flash"]:
                        board.set_rgb_all(COLORS["needs_you"])
                        time.sleep(FLASH_SECS)
                        prev = {}
                    write_slots(st["slots"], st["sessions"], cfg["keys"])
                    prev = apply_paint(board, prev, pulsed(st["paint"], st["pulse"], phase))
            except Exception:
                prev = apply_paint(board, prev, dict(err_paint))
                if stop.wait(min(backoff, 30)):
                    break
                backoff = min(backoff * 2, 30)
    finally:
        board.restore()


def _running():
    try:
        os.kill(int(PIDFILE.read_text().strip()), 0)
        return True
    except Exception:
        return False


def daemon():
    STATE.mkdir(parents=True, exist_ok=True)
    PIDFILE.write_text(str(os.getpid()))
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    try:
        cfg = load_config()
        run(cfg, Kontroll(cfg), stop)
    finally:
        try:
            PIDFILE.unlink()
        except Exception:
            pass


def ensure():
    if _running():
        return
    if os.fork() > 0:
        return
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0); os.dup2(devnull, 1); os.dup2(devnull, 2)
    daemon()
    os._exit(0)


def once():
    cfg = load_config()
    payload = json.loads(Path(os.environ["MOONLIGHT_FEED_FILE"]).read_text())
    st = step({}, payload, cfg)
    write_slots(st["slots"], st["sessions"], cfg["keys"])
    apply_paint(Kontroll(cfg), {}, st["paint"])


def identify():
    cfg = load_config()
    board = Kontroll(cfg)
    for i, led in enumerate(cfg["keys"]):
        print("slot %d = LED %d (lit green now)" % (i, led))
        board.set_rgb(led, "#2ecc71")
        time.sleep(1.2)
        board.set_rgb(led, "#000000")
    board.restore()


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--ensure":
        ensure()
    elif arg == "--once":
        once()
    elif arg == "--identify":
        identify()
    elif arg == "--skhd":
        print_skhd()
    elif arg == "open-slot":
        if len(sys.argv) < 3 or not sys.argv[2].isdigit():
            sys.stderr.write("usage: moonlight open-slot <N>\n")
            sys.exit(2)
        sys.exit(open_slot(int(sys.argv[2])))
    else:
        if _running():
            print("moonlight: daemon already running (pid %s)" % PIDFILE.read_text().strip())
            return
        daemon()


if __name__ == "__main__":
    main()
