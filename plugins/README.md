# romp plugins

Plugins are self-contained programs that connect to the romp kernel over its
local HTTP/WebSocket API. They read fleet status, focus sessions, and drive
external surfaces (keyboards, LEDs, dashboards, bots) without touching romp's
core code. Each plugin is a subdirectory here with its own README and deps.

## The API surface

All endpoints are on `127.0.0.1:29855` (override: `ROMP_SERVE_PORT` env).
Auth: read the token from `~/.local/state/romp/serve-token` (0600) and pass it
as `?token=<token>` on WS connects or `X-Romp-Token: <token>` on HTTP calls.
(Override state root: `ROMP_STATE_DIR` env, else `$XDG_STATE_HOME/romp`.)

### WebSocket: `/ws?app=fleet&wid=<id>&token=<token>`

Push-based, near-real-time (per SDK event + a 0.5–3s backstop). The kernel
sends a JSON message on every change:

```json
{"type": "feed",
 "ledgers": [
   {"sid": "...", "name": "web", "color": "#9cd2ff",
    "status": {"state": "working|awaiting|blocked|compacting|interrupting|retrying|awaitingBg|ready",
               "faded": false, "sinceEpoch": 1753660000000,
               "apiTooLong": false, "apiSpendLimit": false,
               "backend": "sdk", "model": "opus", "effort": "high"}},
   ...
 ],
 "asks": [
   {"sid": "...", "column": "working|needs_input|completed", "text": "...", ...},
   ...
 ],
 "working": ["web", "api"],
 "order": ["web", "api", "tests"],
 ...
}
```

- **`ledgers`**: one entry per live session. `status.state` is the session
  chip — the kernel's single computed status (never re-derive from state files).
  `faded` is true when the session has been idle > 1h.
- **`asks`**: one entry per goal card on the feed board. `column` is the card's
  board lane: `working`, `needs_input` (blocked on you), or `completed`.
  A session can have cards in multiple columns.
- **`order`**: the fleet's shared session order (names), matching the dashboard
  tab/lane order.

### WS messages you can send (client → kernel)

```json
{"type": "openByName", "name": "web"}
```

Un-hides the session's tab, pushes a view update, and focuses the chat clients
on that session. Same effect as clicking a session in the dashboard.

### HTTP

- `GET /healthz` — `200` if the kernel is up (no auth required)
- `GET /sessions` — `[{id, name, state, dir, ...}]` (raw backend state, not
  the computed chip — prefer `ledgers` from the WS for status)
- `POST /send` — `{"name": "web", "text": "..."}` — send input to a session
- `POST /interrupt` — `{"name": "web"}` — interrupt a session's current turn
- `POST /end` — `{"name": "web"}` — end a session

All POST routes return `{"ok": true}` or `{"ok": false, "error": "..."}`.

## Writing a plugin

1. Subscribe to `/ws?app=fleet` for status. The kernel pushes on every change;
   no polling needed.
2. Use only the API above. Never import from `kernel/`, `cli/`, or `postal/` —
   the network boundary is the contract.
3. Manage your own lifecycle (PID file, signal handling, cleanup on exit).
4. Fail loudly when the kernel is unreachable — never silently serve stale
   state.
5. Add a README with setup instructions and a config example.

## Existing plugins

- **[moonlight](moonlight/)** — mirror session status onto a ZSA Moonlander's
  RGB keys; leader-key chord to jump to a session.
