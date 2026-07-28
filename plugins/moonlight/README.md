# moonlight

Mirror romp session status onto a ZSA Moonlander's RGB keys. Each configured
key becomes one session's status lamp; a leader-key chord jumps to the session
a lit key shows.

## Color legend

| LED | meaning |
|---|---|
| yellow, steady | working (dimmed: waiting on background agents — not on you) |
| red, pulsing | needs YOU — a live prompt, or a card in Needs-you |
| red, steady | API error / spend cap |
| teal | compacting |
| green | all goals completed |
| blue, dim | idle (`"idle": "identity"` shows the session's own color instead) |
| white, dim (all keys) | the kernel is unreachable — the board is NOT current |

## Prerequisites

1. **Keymapp** ≥ 1.3.2 — open Settings, enable the API, keep it running with
   the keyboard connected.
2. **kontroll** — [prebuilt binary](https://github.com/zsa/kontroll/releases)
   on your PATH, or `cargo install --git https://github.com/zsa/kontroll`.
   Verify: `kontroll connect-any && kontroll status`.
3. **Python 3** (stdlib only — no pip deps).

## Setup

1. Copy `keyboard.json.example` to `~/.config/romp/keyboard.json` and edit it.

2. Find your LED indices — run `moonlight --identify` and note which physical
   key lights for each index. Update `keys` in the config.

3. **Leader layer (Oryx).** Add a layer with a momentary activation key
   (`MO(n)` on a thumb key). On that layer, map the session keys to
   `F13`, `F14`, … in the same order as `keys`. Flash the layout.

4. **Hotkeys.** `moonlight --skhd >> ~/.config/skhd/skhdrc && skhd --reload`
   (or Hammerspoon/Karabiner: bind F13..Fn to `moonlight open-slot 0..n-1`).

5. **Start.** `moonlight --ensure` (detached daemon; ctrl-c in foreground mode
   restores the keyboard's own lighting).

## Usage

```
moonlight                  # daemon (foreground)
moonlight --ensure         # spawn detached daemon if not running
moonlight --once           # one paint from MOONLIGHT_FEED_FILE (testing)
moonlight --identify       # light each configured LED, print its index
moonlight --skhd           # print skhd bindings for the leader-layer keys
moonlight open-slot N      # focus session on slot N (the leader chord target)
```

## How it works

moonlight is a romp **plugin** — it uses only the kernel's public WebSocket
API (documented in `plugins/README.md`) and imports nothing from romp's code:

- Subscribes to `/ws?app=fleet` for near-real-time status pushes
- Drives LEDs through ZSA's `kontroll` CLI (the Keymapp API)
- Focuses sessions by sending `openByName` over the WS (same verb the
  dashboard uses) + `tmux switch-client` for terminal sessions

## Tests

```
python3 -m pytest plugins/moonlight/test_moonlight.py -v
```

## Troubleshooting

- **All keys dim white**: kernel unreachable — is romp running?
- **No LEDs change**: Keymapp not running / API off / kontroll not on PATH.
- **Chord does nothing**: run `moonlight open-slot 0` by hand — it prints why.
- **LEDs repainted by firmware animation**: set the base layer's RGB mode to
  static/off in Oryx and reflash.
