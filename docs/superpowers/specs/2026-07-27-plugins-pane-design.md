# Plugins pane + moonlight setup UI

A new dashboard pane for managing romp plugins, and a rich setup/monitoring UI
for the moonlight plugin (ZSA keyboard LED mirroring). The user goes from
"never heard of this" to "keys lit up" without touching a terminal or editing
JSON.

## 1. Plugins pane (the container)

### Placement

A new pane in the dashboard's bottom rail, alongside Chat / Timeline / Feed /
Fleet. Route: `/plugins`, WS app: `plugins`. Toggle button in the rail
(`data-pane=plugins`), mobile tab bar entry. Follows the existing iframe +
pane toggle pattern exactly (`_landing()` + `_LANDING_COLLAPSE_JS`).

### Layout: card list → detail

**List view** (the default): one card per discovered plugin. Each card shows:
- Plugin name + one-line description (from `manifest.json`)
- Status dot: green (running), gray (stopped), orange (not configured)
- Click to open the plugin's detail view

**Detail view**: the plugin's own UI fills the pane (for moonlight: the
keyboard diagram). A back arrow returns to the list. The detail view is
plugin-specific HTML/JS served from the plugin's directory.

### Plugin discovery

At startup (and on a new `/plugins` route hit), the kernel scans the
`plugins/` directory for subdirectories containing a `manifest.json`:

```json
{
  "name": "moonlight",
  "description": "Mirror session status onto ZSA keyboard RGB keys",
  "entry": "moonlight.py",
  "ui": "ui.html",
  "configPath": "~/.config/romp/keyboard.json",
  "pidFile": "moonlight.pid"
}
```

- `entry`: the executable the kernel spawns for start/stop.
- `ui`: the HTML file the detail view loads (served at
  `/plugins/moonlight/ui.html`). If absent, the detail view shows only
  status + start/stop + config path.
- `configPath`: where the plugin reads its config; the kernel checks existence
  to derive the "configured" status.
- `pidFile`: filename under `$STATE/`; the kernel checks it for running status
  (same `kill(pid, 0)` pattern as `idle_dots.py`).

No npm deps, no build step for plugin UIs — plain HTML + inline JS + inline
CSS, served as static files by the kernel. A plugin UI communicates with the
kernel via the same WS/HTTP API any external client uses.

### Plugin lifecycle

The kernel manages start/stop via the manifest's `entry`:

- **Start**: the kernel runs
  `python3 plugins/<name>/<entry> --ensure` (the double-fork daemon pattern),
  from the repo root. The plugin writes its own PID file.
- **Stop**: the kernel reads the PID file and sends `SIGTERM`. The plugin's
  signal handler cleans up (moonlight restores LEDs).
- **Status**: PID file exists + `kill(pid, 0)` succeeds → running; PID file
  exists but process gone → stopped (stale PID); no PID file → stopped; no
  config file → not configured.

The start/stop button in the detail view (and a smaller one on the card) POSTs
to a new kernel route `POST /plugin` with body
`{"name": "moonlight", "action": "start"|"stop"}`, returning
`{"ok": true, "status": "running"|"stopped"}`.

### Data flow

The plugins pane subscribes as `app=plugins`. The kernel's push loop sends a
lightweight payload on each push cycle:

```json
{"type": "plugins",
 "plugins": [
   {"name": "moonlight", "description": "...",
    "status": "running|stopped|not_configured",
    "hasUi": true, "pid": 12345}
 ],
 "ledgers": [...]}
```

`ledgers` (the fleet session status) is included so plugin UIs that need
session data (moonlight's live diagram) get it without opening a second WS.
The kernel already builds `ledgers` for the fleet push; bundling it here is
near-free.

## 2. Moonlight plugin UI

### The keyboard diagram

An inline SVG of the Moonlander's split layout — both halves, all 72 keys in
their physical positions. LED indices are baked in from QMK's fixed
`led_config` mapping (the same for every Moonlander). The user never sees an
index number.

Three visual states per key:

| state | SVG appearance | real LED |
|---|---|---|
| unselected | dark fill, subtle border | off |
| selected, no session | accent-blue outline, empty | green flash on click (confirms kontroll works) |
| selected, session mapped | filled with status color, session name label | status color (from the daemon) |

The diagram lives in `plugins/moonlight/ui.html` — a single self-contained
HTML file with inline SVG, JS, and CSS. It communicates with the kernel WS for
session data and with the moonlight daemon for kontroll commands (or shells out
to kontroll directly via a small kernel proxy route — see below).

### Setup flow (first time)

The user clicks moonlight's card, which opens the detail view. The detail view
runs through prerequisites automatically:

**Step 1 — kontroll check.**
The UI asks the kernel to check for `kontroll` on PATH
(`POST /plugin {"name":"moonlight","action":"check-deps"}`).
- Present → green check, proceed.
- Missing → "Installing kontroll..." The kernel downloads the prebuilt binary
  from `https://github.com/zsa/kontroll/releases` (the macOS arm64 asset) into
  `~/.local/bin/` and adds it to the daemon's PATH. Progress bar. On success,
  green check. On failure (network, permissions), show the manual install
  command as a fallback.

**Step 2 — Keymapp API check.**
The kernel attempts to connect to `~/.keymapp/keymapp.sock`.
- Responding → green check, proceed.
- Socket missing / not responding → show an inline guide: "Open Keymapp →
  click Settings (gear icon) → enable 'API'" with a screenshot of the Keymapp
  settings screen highlighting the toggle. A "Check again" button re-probes.

**Step 3 — Key selection (the diagram).**
Both deps green → the Moonlander SVG appears with all keys unselected:
- Click a key to select it as a session slot. On click:
  - The SVG key turns accent-blue
  - The real physical LED flashes green via kontroll (immediate feedback)
  - The selection is persisted to `~/.config/romp/keyboard.json` live (no
    save button — every click writes, the daemon picks up changes)
- Click again to deselect
- Below the diagram: a summary line ("6 keys selected — ready to start")
  and the Start button (which runs `--ensure`)

**Step 4 — Running.**
The Start button starts the daemon. The diagram transitions to live mode:
selected keys fill with session status colors and show session names. The
setup steps collapse to a "Prerequisites: OK" chip (expandable if they want
to re-check).

### Live view (ongoing)

Once running, the detail view shows:
- The diagram with live session assignments + status colors, updating in
  real-time from the fleet WS payload
- Status badge (running + uptime), stop button
- Click a mapped key → a small popover:
  - Session name + status
  - "Pin to this key" / "Unpin" toggle (writes to config)
  - "Jump to session" button (sends `openByName` over WS)
- Click an unmapped selected key → same popover but with a dropdown to pin
  a specific session to it

### Key selection persistence

The diagram writes `~/.config/romp/keyboard.json` directly (the same file the
daemon reads). The `keys` array, `pins` map, and `idle` mode are all settable
from the UI. The daemon's config-reload path picks up changes on the next feed
push (it re-reads config per push cycle, or on SIGHUP).

### Kontroll proxy

The UI needs to light individual LEDs during setup (the green flash on key
selection). It cannot shell out to `kontroll` from the browser. Two options:

**Option A (chosen):** a kernel proxy route
`POST /plugin {"name":"moonlight","action":"light-led","led":33,"color":"#2ecc71"}`
that the kernel executes as `kontroll set-rgb 33 --color #2ecc71`. Thin,
scoped, reuses the plugin action route.

**Option B (rejected):** the daemon exposes its own local HTTP server. Adds
complexity; the daemon is a WS client, not a server.

### SVG source

The Moonlander SVG is a simplified outline of the keyboard's physical layout
(two halves, thumb clusters, the split angle). Each key is a `<rect>` or
`<path>` with `data-led="N"` carrying its LED index. The drawing is
hand-authored to match the Moonlander's proportions — not a photograph, not
the Oryx configurator (which is copyrighted). Future keyboards (Voyager) get
their own SVG; the UI loads the right one based on `kontroll status` output
(which reports the connected model).

## 3. Kernel changes

All changes are additive — no modifications to existing routes or push logic.

- **New route: `GET /plugins`** — serves the plugins pane HTML (like
  `_feed_page()`).
- **New route: `GET /plugins/<name>/<file>`** — serves static files from a
  plugin's directory (the `ui.html`, any assets). Path-traversal guarded.
- **New route: `POST /plugin`** — plugin actions (start, stop, check-deps,
  install-deps, light-led). Auth: serve token.
- **New WS app: `plugins`** — push cycle sends `{type:"plugins", plugins:[...],
  ledgers:[...]}`.
- **Plugin scanner** — `_scan_plugins()` reads `plugins/*/manifest.json` at
  startup, caches the list, re-scans on `POST /plugin` or when the pane
  connects.
- **Landing shell** — add the plugins pane iframe, rail button, mobile tab,
  and pane-toggle CSS/JS (same pattern as existing panes).
- **esbuild** — no changes (the plugin UI is plain HTML, not a TypeScript
  bundle).

## 4. File structure

```
plugins/
  README.md                          # plugin API spec (already exists)
  moonlight/
    manifest.json                    # plugin metadata (new)
    moonlight.py                     # the daemon (already exists)
    ui.html                          # setup + live diagram (new)
    moonlander.svg                   # keyboard outline (new, inline in ui.html)
    keyboard.json.example            # starter config (already exists)
    test_moonlight.py                # daemon tests (already exists)
    README.md                        # setup guide (already exists)
kernel/
  kernel.py                          # new routes + push app + scanner
```

## 5. Testing

- **Plugin scanner**: unit tests for `_scan_plugins()` — valid manifest,
  missing manifest, malformed JSON. Source-pinned (no kernel import).
- **Plugin status**: unit tests for PID-file status derivation.
- **POST /plugin actions**: bats tests for start/stop/check-deps (mock
  kontroll + mock plugin entry).
- **Moonlight UI**: the diagram's click-to-select and live-update logic is
  plain JS in `ui.html`; a `ui/webview/moonlight.test.ts` covers the
  state machine (selected keys, pin/unpin, status color mapping).
- **kontroll proxy**: bats test that the light-led action calls kontroll with
  the right args (mock binary).
- **Existing suites**: no regressions — the changes are additive.

## 6. Out of scope

- Multiple keyboard models (Voyager SVG — future, when someone needs it)
- Plugin marketplace / remote plugin install
- Plugin-to-plugin communication
- Custom plugin settings schema (plugins manage their own config files)
- Oryx layer setup guidance (the leader-key chord is a separate concern —
  the skhd bindings + Oryx instructions stay in the README)
