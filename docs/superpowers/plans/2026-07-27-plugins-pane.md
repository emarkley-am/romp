# Plugins Pane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A new dashboard pane (card list → detail) for managing romp plugins, with plugin discovery from `manifest.json`, start/stop lifecycle, and a WS push stream — so the moonlight plugin (and future plugins) can be configured and controlled without touching a terminal.

**Architecture:** The kernel scans `plugins/*/manifest.json` at startup for registered plugins. A new `POST /plugin` route handles start/stop/check-deps/light-led actions. The plugins pane subscribes as `app=plugins` and receives `{type:"plugins", plugins:[...], ledgers:[...]}` on each push. Plugin UIs are plain HTML files served from the plugin's directory. The pane follows the existing iframe + rail-toggle pattern exactly.

**Tech Stack:** Python (kernel routes + scanner), inline HTML/CSS/JS (pane page, no TypeScript bundle — follows the gear.js precedent for self-contained pages).

## Global Constraints

- **No TypeScript bundle** for the plugins pane — it's inline JS in a kernel-served HTML page (like the gear modal). Plugin UIs are plain HTML files served as static assets. No esbuild changes.
- **kernel.py is 16k+ lines** — follow the existing style: inline HTML strings, inline CSS, comment-dense with WHY. No imports from plugins/.
- **Plugin UIs must be fully self-contained** — they communicate with the kernel only via the WS/HTTP API documented in `plugins/README.md`. They import nothing from romp internals.
- **Existing push loop is hot path** — the plugins payload must be cheap (PID file stat + config file exists, no subprocess calls per push).
- **Privacy**: synthetic test data only — `TESTHOST`, `11111111-...` sids.
- **Pane toggle pattern** (`_LANDING_COLLAPSE_JS`): panes are body classes (`po-<name>`), persisted to localStorage, default off for new panes.

---

### Task 1: Plugin scanner + manifest

**Files:**
- Create: `plugins/moonlight/manifest.json`
- Modify: `kernel/kernel.py` (add `_scan_plugins()` + `_plugin_status()`, near the plugin routes area ~line 15860)
- Test: `tests/test_plugin_scanner.py`

**Interfaces:**
- Produces: `_scan_plugins() -> list[dict]` returning `[{"name","description","entry","ui","configPath","pidFile","dir"}]`, `_plugin_status(plugin) -> "running"|"stopped"|"not_configured"`.

- [ ] **Step 1: Create manifest.json**

`plugins/moonlight/manifest.json`:
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

- [ ] **Step 2: Write the failing tests**

`tests/test_plugin_scanner.py`:
```python
#!/usr/bin/env python3
"""Plugin scanner — discovers plugins from manifest.json files and derives
their status from PID files + config existence. Source-pinned against the
kernel (importing it runs boot reconcile — tests/README)."""
import json
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
ROOT = os.path.dirname(HERE)


class ScannerSourcePin(unittest.TestCase):
    """The kernel must contain the scanner + status functions."""
    def setUp(self):
        self.src = open(os.path.join(ROOT, "kernel", "kernel.py")).read()

    def test_scan_plugins_present(self):
        self.assertIn("def _scan_plugins(", self.src)

    def test_plugin_status_present(self):
        self.assertIn("def _plugin_status(", self.src)

    def test_plugin_post_route(self):
        self.assertIn('u.path == "/plugin"', self.src)

    def test_plugins_page_route(self):
        self.assertIn('p == "/plugins"', self.src)
        self.assertIn("_plugins_page()", self.src)


class ManifestShape(unittest.TestCase):
    """The moonlight manifest exists and has the required fields."""
    def test_manifest_valid(self):
        with open(os.path.join(ROOT, "plugins", "moonlight", "manifest.json")) as f:
            m = json.load(f)
        for key in ("name", "description", "entry", "pidFile"):
            self.assertIn(key, m, "manifest missing %s" % key)
        self.assertEqual(m["name"], "moonlight")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run tests to verify failure**

Run: `cd /Users/emarkley/Code/romp-moonlight && python3 -m pytest tests/test_plugin_scanner.py -v`
Expected: `ManifestShape` passes (manifest exists), `ScannerSourcePin` fails (no functions in kernel yet).

- [ ] **Step 4: Implement the scanner in kernel.py**

Add after the existing `_feed_goals` / before `build_feed` area (or near the `/sessions` route), a self-contained block:

```python
# ───────────────────────── plugin discovery + lifecycle ─────────────────────────
_plugins_cache = [None]   # [list] — refreshed on /plugin POST or first /plugins connect

def _scan_plugins():
    """Read plugins/*/manifest.json — the ONLY contract between the kernel and a
    plugin. A missing or malformed manifest silently skips the directory (a WIP
    plugin that hasn't shipped its manifest yet is invisible, not an error)."""
    out = []
    pdir = ROOT / "plugins"
    if not pdir.is_dir():
        return out
    for d in sorted(pdir.iterdir()):
        mf = d / "manifest.json"
        if not mf.is_file():
            continue
        try:
            m = json.loads(mf.read_text())
            m["dir"] = str(d)
            out.append(m)
        except Exception:
            pass
    _plugins_cache[0] = out
    return out


def _plugin_status(plugin):
    """running | stopped | not_configured — derived from the PID file and the
    config file, both cheap stats (no subprocess calls, safe for the push loop)."""
    cfg = plugin.get("configPath", "")
    if cfg:
        cp = Path(os.path.expanduser(cfg))
        if not cp.exists():
            return "not_configured"
    pf = plugin.get("pidFile")
    if not pf:
        return "stopped"
    pidpath = STATE / pf
    if not pidpath.exists():
        return "stopped"
    try:
        pid = int(pidpath.read_text().strip())
        os.kill(pid, 0)
        return "running"
    except Exception:
        return "stopped"
```

- [ ] **Step 5: Run tests to verify pass**

Run: `python3 -m pytest tests/test_plugin_scanner.py -v` — Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add plugins/moonlight/manifest.json kernel/kernel.py tests/test_plugin_scanner.py
git commit -m "feat: plugin scanner — manifest.json discovery + PID-based status"
```

---

### Task 2: POST /plugin route (start, stop, check-deps, light-led)

**Files:**
- Modify: `kernel/kernel.py` (add `POST /plugin` handler in `do_POST`, after the `/open` or `/new` block ~line 15863)
- Test: `tests/test_plugin_scanner.py` (source-pin additions), `tests/romp.bats` (curl-based)

**Interfaces:**
- Consumes: `_scan_plugins()`, `_plugin_status()` from Task 1.
- Produces: `POST /plugin` accepting `{"name":"moonlight","action":"start"|"stop"|"check-deps"|"light-led","led":N,"color":"#hex"}`, returning `{"ok":true,"status":"running"}` etc.

- [ ] **Step 1: Add failing source-pin tests** (append to `tests/test_plugin_scanner.py`)

```python
class PluginRouteShape(unittest.TestCase):
    def setUp(self):
        self.src = open(os.path.join(ROOT, "kernel", "kernel.py")).read()

    def test_start_action(self):
        i = self.src.find('u.path == "/plugin"')
        self.assertGreater(i, -1)
        block = self.src[i:i + 3000]
        self.assertIn('"start"', block)
        self.assertIn('"stop"', block)
        self.assertIn('SIGTERM', block)
        self.assertIn('"check-deps"', block)
        self.assertIn('"light-led"', block)
```

- [ ] **Step 2: Run to verify failure**, then **Step 3: Implement**

Add in `do_POST`, after the existing routes:

```python
            if u.path == "/plugin":
                # Plugin lifecycle + utility actions (the plugins pane —
                # docs/superpowers/specs/2026-07-27-plugins-pane-design.md).
                try:
                    b = json.loads(raw_body or b"{}")
                except Exception:
                    return self._send(400, json.dumps({"ok": False, "error": "bad JSON"}), "application/json")
                name = str(b.get("name", ""))
                action = str(b.get("action", ""))
                plugins = _plugins_cache[0] or _scan_plugins()
                plugin = next((p for p in plugins if p.get("name") == name), None)
                if not plugin:
                    return self._send(200, json.dumps({"ok": False, "error": "unknown plugin %r" % name}), "application/json")
                if action == "start":
                    entry = Path(plugin["dir"]) / plugin.get("entry", "")
                    if not entry.is_file():
                        return self._send(200, json.dumps({"ok": False, "error": "entry %s not found" % entry}), "application/json")
                    subprocess.Popen([sys.executable, str(entry), "--ensure"],
                                     cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    time.sleep(0.3)   # let the double-fork settle so status reads "running"
                    return self._send(200, json.dumps({"ok": True, "status": _plugin_status(plugin)}), "application/json")
                if action == "stop":
                    pf = plugin.get("pidFile")
                    if pf:
                        pidpath = STATE / pf
                        try:
                            pid = int(pidpath.read_text().strip())
                            os.kill(pid, signal.SIGTERM)
                        except Exception:
                            pass
                    time.sleep(0.3)
                    return self._send(200, json.dumps({"ok": True, "status": _plugin_status(plugin)}), "application/json")
                if action == "check-deps":
                    kontroll = shutil.which("kontroll")
                    keymapp_sock = Path.home() / ".keymapp" / "keymapp.sock"
                    return self._send(200, json.dumps({"ok": True,
                        "kontroll": bool(kontroll), "kontrollPath": kontroll or "",
                        "keymapp": keymapp_sock.exists()}), "application/json")
                if action == "light-led":
                    led = b.get("led")
                    color = b.get("color", "#2ecc71")
                    if led is None:
                        return self._send(400, json.dumps({"ok": False, "error": "led required"}), "application/json")
                    kontroll = shutil.which("kontroll")
                    if not kontroll:
                        return self._send(200, json.dumps({"ok": False, "error": "kontroll not found"}), "application/json")
                    try:
                        subprocess.run([kontroll, "set-rgb", str(led), "--color", color],
                                       capture_output=True, timeout=5)
                    except Exception as e:
                        return self._send(200, json.dumps({"ok": False, "error": str(e)}), "application/json")
                    return self._send(200, json.dumps({"ok": True}), "application/json")
                return self._send(400, json.dumps({"ok": False, "error": "unknown action %r" % action}), "application/json")
```

Add `import shutil` to the kernel's imports if not already present (check first — `grep -n "import shutil" kernel/kernel.py`).

- [ ] **Step 4: Run all** — `python3 -m pytest tests/test_plugin_scanner.py -v` → PASS.
- [ ] **Step 5: Commit** — `git commit -am "feat: POST /plugin — start, stop, check-deps, light-led"`

---

### Task 3: Plugins pane page + landing integration

**Files:**
- Modify: `kernel/kernel.py`:
  - Add `_plugins_page()` function (near `_feed_page()` ~line 14126)
  - Add `GET /plugins` route (in `do_GET` ~line 16002)
  - Add `GET /plugins/<name>/<file>` static file route (after `/dist/` ~line 16018)
  - Add plugins pane iframe + rail button to `_landing()` (~line 15558-15580)
  - Add `plugins` to `_LANDING_COLLAPSE_JS` pane object (~line 15061)
  - Add show/hide CSS for `po-plugins` (~line 15417)
  - Add mobile tab button (~line 15615-15619)
- Test: `tests/test_plugin_scanner.py` (source-pin additions)

**Interfaces:**
- Consumes: `_scan_plugins()`, `_plugin_status()` from Task 1.
- Produces: `/plugins` page, `/plugins/<name>/<file>` static serve, landing pane + toggle.

- [ ] **Step 1: Add failing tests**

```python
class PaneIntegration(unittest.TestCase):
    def setUp(self):
        self.src = open(os.path.join(ROOT, "kernel", "kernel.py")).read()

    def test_landing_has_plugins_pane(self):
        self.assertIn("plugins-pane", self.src)
        self.assertIn("data-pane=plugins", self.src)

    def test_collapse_js_knows_plugins(self):
        i = self.src.find("var PK='romp-panes'")
        block = self.src[i:i + 500]
        self.assertIn("plugins", block)

    def test_plugins_static_route(self):
        self.assertIn('p.startswith("/plugins/")', self.src)
```

- [ ] **Step 2: Implement `_plugins_page()`**

```python
def _plugins_page():
    """The plugins pane — a card list of discovered plugins with status + start/stop,
    detail view loads the plugin's ui.html. Plain inline JS, no bundle (follows the
    gear.js precedent for self-contained pages)."""
    v = _dist_ver()
    plugins_json = json.dumps([
        {"name": p.get("name", ""), "description": p.get("description", ""),
         "status": _plugin_status(p), "hasUi": bool(p.get("ui")),
         "ui": "/plugins/%s/%s" % (p["name"], p["ui"]) if p.get("ui") else None}
        for p in (_plugins_cache[0] or _scan_plugins())
    ])
    return ("<!DOCTYPE html><html lang=en><head><meta charset=UTF-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<link rel=icon type=image/svg+xml href=/media/romp-swirl-glyph.svg>"
            "<title>Romp · plugins</title>"
            "<style>" + THEME_CSS + "\n" + _PLUGINS_CSS + "</style></head>"
            "<body><div id=plugins-root></div>"
            "<script>%s</script>"
            "<script>" + _PLUGINS_JS + "</script></body></html>"
            % _shim("plugins", v))
```

- [ ] **Step 3: Write the inline CSS (`_PLUGINS_CSS`)**

```python
_PLUGINS_CSS = (
    "body{margin:0;font:13px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
    "color:#cfd6dd;background:var(--vscode-editor-background,#1e1e1e)}"
    "#plugins-root{padding:12px 16px}"
    ".pl-head{font-size:15px;font-weight:600;margin-bottom:12px;color:#e8eef5}"
    ".pl-card{display:flex;align-items:center;gap:10px;padding:10px 12px;"
    "border:1px solid #333;border-radius:8px;margin-bottom:8px;cursor:pointer;"
    "transition:border-color .15s}"
    ".pl-card:hover{border-color:#555}"
    ".pl-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}"
    ".pl-dot.running{background:#2ecc71}.pl-dot.stopped{background:#666}"
    ".pl-dot.not_configured{background:#e0a020}"
    ".pl-name{font-weight:600;color:#e8eef5}.pl-desc{opacity:.7;font-size:12px}"
    ".pl-info{flex:1;min-width:0}"
    ".pl-btn{font:inherit;cursor:pointer;border-radius:6px;padding:5px 11px;"
    "border:1px solid #444;background:#2a2d31;color:#cfd6dd;white-space:nowrap}"
    ".pl-btn:hover{border-color:#666;background:#333}"
    ".pl-btn.accent{background:var(--accent,#9cd2ff);color:var(--accent-fg,#0c1a2e);"
    "border-color:transparent;font-weight:600}"
    ".pl-back{cursor:pointer;opacity:.7;margin-bottom:8px;font-size:12px}"
    ".pl-back:hover{opacity:1}"
    ".pl-detail{margin-top:8px}"
    ".pl-detail iframe{width:100%;border:none;border-radius:6px;"
    "background:var(--vscode-editor-background,#1e1e1e)}"
    ".pl-status{display:flex;align-items:center;gap:8px;margin-bottom:12px}"
)
```

- [ ] **Step 4: Write the inline JS (`_PLUGINS_JS`)**

```python
_PLUGINS_JS = """
(function(){
  var root = document.getElementById('plugins-root');
  var plugins = [];
  var detail = null;   // name of the plugin in detail view, or null for list

  function dot(s) { return '<span class="pl-dot '+s+'"></span>'; }
  function statusLabel(s) { return s === 'not_configured' ? 'not configured' : s; }

  function renderList() {
    var h = '<div class=pl-head>Plugins</div>';
    if (!plugins.length) { h += '<div style="opacity:.5">No plugins found in plugins/</div>'; }
    plugins.forEach(function(p) {
      h += '<div class=pl-card data-name="'+p.name+'">'
         + dot(p.status)
         + '<div class=pl-info><div class=pl-name>'+p.name+'</div>'
         + '<div class=pl-desc>'+p.description+'</div></div>'
         + (p.status==='running'
            ? '<button class="pl-btn" data-act=stop data-name="'+p.name+'">Stop</button>'
            : p.status==='stopped'
            ? '<button class="pl-btn accent" data-act=start data-name="'+p.name+'">Start</button>'
            : '')
         + '</div>';
    });
    root.innerHTML = h;
  }

  function renderDetail(name) {
    var p = plugins.find(function(x){return x.name===name;});
    if (!p) { detail = null; renderList(); return; }
    var h = '<div class=pl-back data-back=1>← Plugins</div>'
          + '<div class=pl-status>' + dot(p.status)
          + '<span class=pl-name>' + p.name + '</span>'
          + '<span style="opacity:.6"> — ' + statusLabel(p.status) + '</span>'
          + (p.status==='running'
             ? '<button class="pl-btn" data-act=stop data-name="'+p.name+'">Stop</button>'
             : '<button class="pl-btn accent" data-act=start data-name="'+p.name+'">Start</button>')
          + '</div>';
    if (p.hasUi && p.ui) {
      h += '<div class=pl-detail><iframe id=plugin-ui src="'+p.ui+'" style="height:calc(100vh - 120px)"></iframe></div>';
    }
    root.innerHTML = h;
  }

  function postAction(name, action, extra) {
    var body = Object.assign({name: name, action: action}, extra || {});
    fetch('/plugin', {method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify(body)})
      .then(function(r){return r.json();})
      .then(function(d){
        if (d.status) {
          var p = plugins.find(function(x){return x.name===name;});
          if (p) p.status = d.status;
          if (detail === name) renderDetail(name); else renderList();
        }
      });
  }

  root.addEventListener('click', function(e) {
    var t = e.target;
    if (t.dataset && t.dataset.back) { detail = null; renderList(); return; }
    if (t.dataset && t.dataset.act) {
      e.stopPropagation();
      postAction(t.dataset.name, t.dataset.act);
      return;
    }
    var card = t.closest && t.closest('.pl-card');
    if (card && card.dataset.name) { detail = card.dataset.name; renderDetail(detail); }
  });

  // WS data: update plugin list on each push
  if (window.__rompOnMessage) {
    window.__rompOnMessage(function(msg) {
      if (msg && msg.type === 'plugins' && msg.plugins) {
        plugins = msg.plugins;
        if (detail) renderDetail(detail); else renderList();
      }
    });
  }

  // initial load from the baked-in data (server-rendered)
  try { plugins = JSON.parse(document.getElementById('pl-init').textContent); } catch(e) {}
  renderList();
})();
"""
```

Wait — the initial data should be embedded. Adjust `_plugins_page()` to include:
```python
"<script id=pl-init type=application/json>%s</script>" % plugins_json
```
inside the `<body>` before the JS scripts.

- [ ] **Step 5: Add the WS `app=plugins` push**

In the push loop (`_push`, ~line 13548), after the feed/timeline sends:

```python
    want_plugins = any(c["app"] == "plugins" for c in targets)
    if want_plugins:
        pl = [{"name": p.get("name", ""), "description": p.get("description", ""),
               "status": _plugin_status(p), "hasUi": bool(p.get("ui")),
               "ui": "/plugins/%s/%s" % (p["name"], p["ui"]) if p.get("ui") else None}
              for p in (_plugins_cache[0] or _scan_plugins())]
        plugins_msg = {"type": "plugins", "plugins": pl}
        if feed is not None and "ledgers" in feed:
            plugins_msg["ledgers"] = feed["ledgers"]
        for c in targets:
            if c["app"] == "plugins":
                _send_client(c, ("plugins",), plugins_msg)
```

- [ ] **Step 6: Add the routes to `do_GET`**

After the `/fleet` route (~line 16017):
```python
            if p == "/plugins":
                _client_seen[0] = time.time()
                return self._send(200, _plugins_page(), "text/html; charset=utf-8", cache="no-cache")
            if p.startswith("/plugins/"):
                # Serve static files from a plugin's directory (ui.html, assets).
                # Path: /plugins/<name>/<file> → plugins/<name>/<file>
                parts = p.split("/", 3)   # ['', 'plugins', name, file]
                if len(parts) == 4:
                    fp = (ROOT / "plugins" / parts[2] / parts[3]).resolve()
                    pbase = (ROOT / "plugins" / parts[2]).resolve()
                    if pbase in fp.parents or fp == pbase:
                        if fp.is_file():
                            ct = {"html": "text/html", "js": "text/javascript", "css": "text/css",
                                  "svg": "image/svg+xml", "json": "application/json",
                                  "png": "image/png"}.get(fp.suffix.lstrip("."), "text/plain")
                            return self._send(200, fp.read_bytes(), ct + "; charset=utf-8", cache="no-cache")
                return self._send(404, "not found", "text/plain")
```

- [ ] **Step 7: Add the pane to the landing**

In `_landing()`, add after the feed pane (~line 15564):
```python
"<div class=pane id=plugins-pane><iframe id=f-plugins src=/plugins></iframe></div>"
```

Add the rail button (~line 15580, after the Feed button):
```python
"<div class=rail-btn data-pane=plugins>Plugins</div>"
```

Add the mobile tab (~line 15619, after Timeline):
```python
"<button data-pane=plugins>Plugins</button>"
```

In `_LANDING_COLLAPSE_JS` (~line 15061), add `plugins:false` to the `po` default:
```python
"var PK='romp-panes',po={chat:true,fleet:false,feed:true,timeline:true,plugins:false};"
```

Add the `LBL` entry:
```python
"var LBL={chat:'chat',fleet:'fleet',feed:'feed',timeline:'timeline',plugins:'plugins'};"
```

Add the CSS hide rule (~line 15417):
```python
"body:not(.po-plugins) #plugins-pane{display:none}"
```

Add the body class toggle in the `apply()` function:
```python
"document.body.classList.toggle('po-plugins',!!po.plugins);"
```

The `__rompOnMessage` hook the JS uses needs to be exposed by the shim. Check if it already exists — if not, add to `_shim()`:
```python
"window.__rompOnMessage=function(fn){_msgCbs.push(fn);};"
```
And in the shim's `onmessage`, call each registered callback:
```python
"_msgCbs.forEach(function(fn){try{fn(msg);}catch(e){}});"
```
(Check the existing shim code carefully — it may already have a message dispatch mechanism the plugin page can hook into.)

- [ ] **Step 8: Run tests** — `python3 -m pytest tests/test_plugin_scanner.py -v` → all PASS. Also manually: open the dashboard, see the Plugins toggle in the rail, click it, see the moonlight card.
- [ ] **Step 9: Commit** — `git commit -am "feat: plugins pane — card list, start/stop, static serving, landing integration"`

---

### Task 4: Shim `__rompOnMessage` hook + WS reconnect for plugin pages

**Files:**
- Modify: `kernel/kernel.py` (`_shim()` function)
- Test: manual (the shim is inline JS in a server-rendered page; no unit-test seam without a browser)

**Interfaces:**
- Produces: `window.__rompOnMessage(callback)` — registers a function called with each parsed WS message. Available in every shim-equipped page.

- [ ] **Step 1: Read `_shim()`** — find the WS `onmessage` handler. Locate where the received JSON is parsed and dispatched.

- [ ] **Step 2: Add the hook** — after the parse, add:
```javascript
var _msgCbs = [];
window.__rompOnMessage = function(fn) { _msgCbs.push(fn); };
// In the onmessage handler, after parsing:
_msgCbs.forEach(function(fn) { try { fn(msg); } catch(e) {} });
```

If the shim already has a dispatch mechanism (e.g., `postMessage` to parent), add the callback array alongside it — don't replace the existing mechanism.

- [ ] **Step 3: Verify** — open the dashboard, toggle Plugins on, confirm the card list renders and updates when the daemon starts/stops.
- [ ] **Step 4: Commit** — `git commit -am "feat: shim __rompOnMessage hook for plugin pages"`

---

### Task 5: Full suite verification + docs

**Files:**
- Modify: `plugins/README.md` (add note about manifest.json and lifecycle)
- Test: full suite run

**Interfaces:**
- Consumes: all prior tasks.

- [ ] **Step 1: Run full test suite**

```bash
python3 -m pytest tests/ -q
python3 -m pytest plugins/moonlight/test_moonlight.py -q
npx --yes bats tests/romp.bats
```

All must pass with no regressions.

- [ ] **Step 2: Update `plugins/README.md`** — add a section on manifest.json:

```markdown
## Plugin registration

Each plugin directory must contain a `manifest.json`:

\`\`\`json
{
  "name": "moonlight",
  "description": "Mirror session status onto ZSA keyboard RGB keys",
  "entry": "moonlight.py",
  "ui": "ui.html",
  "configPath": "~/.config/romp/keyboard.json",
  "pidFile": "moonlight.pid"
}
\`\`\`

- `entry`: the Python file the kernel spawns with `--ensure` for start.
- `ui` (optional): an HTML file served at `/plugins/<name>/<file>` and loaded
  in the Plugins pane's detail view. Plain HTML + inline JS/CSS — no build step.
- `configPath`: the plugin's config file; the kernel checks existence to
  derive "not configured" status.
- `pidFile`: filename under the state dir; the kernel checks it for
  running/stopped status.
```

- [ ] **Step 3: Commit** — `git add -u && git commit -m "docs: plugin manifest spec + full suite verification"`

---

## Verification (whole feature)

- `python3 -m pytest tests/ -q` — no regressions.
- `python3 -m pytest plugins/moonlight/test_moonlight.py -q` — plugin tests.
- `npx --yes bats tests/romp.bats` — shell surface.
- Manual: open the dashboard → Plugins toggle in rail → card list shows moonlight → click Start → daemon runs → click the card → detail view loads.
