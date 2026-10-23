#!/usr/bin/env python3
"""previewapk — push an APK to your PreviewAPK device, get a preview link.

Made for developers, CI jobs, and AI agents. No dependencies (stdlib only).

Setup:
  previewapk setup            agent-led setup: the portal mints a key and
                              this command saves it to
                              ~/.previewapk/credentials.json (0600) after
                              you approve a short code in your browser —
                              no key is ever pasted into a chat.
  # or manually: export PREVIEWAPK_API_KEY=<your key from the portal's
  # Developers card> (the env var wins over the saved credential)
  # optional: PREVIEWAPK_HOST (default https://previewapk.siteport.site)
  # PREVIEWAPK_DEVICE pins a device; without it every command needs --device

Commands:
  previewapk setup [--no-wait] [--label "..."]   set this machine up (see above)
  previewapk push app.apk [--run] [--driver agent|user] [--device dev1]
  previewapk start [--driver agent|user]     start the device's attached app
  previewapk stop                             stop the device (the session link dies)
  previewapk status                           device + session state
  previewapk links                            the share link for the live session
  previewapk tap X Y                          X,Y are 0..1 screen fractions
  previewapk tap --ref N --snapshot ID        tap the element a `look` snapshot numbered N (fails stale_snapshot if it moved)
  previewapk swipe X1 Y1 X2 Y2 [MS]
  previewapk key CODE                         Android keycode (4=Back, 3=Home)
  previewapk text "hello"                     type into the focused field
  previewapk look                             compact numbered snapshot of the screen (refs, types, centers) — observe before acting
  previewapk act batch.json                   run a batch of up to 20 actions in order (JSON file or stdin); stops at the first failure, never replays
  previewapk wait --text "Welcome"            wait until an element appears (or --gone: disappears); exit 3 on timeout
  previewapk wait --for driver [--timeout 300]  wait until the human hands the wheel back to the agent (after request-human or a takeover); exit 0 on handback (prints the handback note if the human left one), 3 on timeout
  previewapk tree                             the UI element tree (text + tap coordinates) — the fast, reliable way to verify what is on screen
  previewapk shot [out.png] [--scale 0.5]     save a screenshot (resumable download; --scale shrinks it for weak networks)
  previewapk relaunch                         bring the app back to the foreground (no reboot)
  previewapk handoff user|agent               pass the wheel mid-session
  previewapk request-human "reason"           ask the human for a step only a human should do (password, 2FA, payment, CAPTCHA): records the request in the portal and prints the exact message to relay to them; then wait --for driver
  previewapk finish --summary "..." [--verdict passed|failed|mixed] [--end-session]   file the session wrap-up — the counts (duration, actions, errors) come from the server; prints the exact message to relay to the human. Default: the session keeps running
  previewapk feedback --broke "..." --wish "..."   file a field report: what broke, what worked, what you wish it had — read and tallied by the team

Reliability: if a connection drops mid-action, the CLI checks the
device before reporting — an action is only called failed when the
state check says it did not land. Exit code 2 means "outcome unknown —
verify with status/tree", never a silent guess.

Add --json to any command for machine-readable output.

Driver modes: a session is driven by exactly one side. `--driver agent`
means THIS caller (the API key) may drive; the owner watches in the portal.
`--driver user` means the owner drives in the portal and API input is
refused. `handoff` switches sides on a live session without rebooting.
"""
import argparse
import hashlib
import http.client
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

VERSION = "1.1.1"
HOST = os.environ.get("PREVIEWAPK_HOST", "https://previewapk.siteport.site").rstrip("/")

CRED_FILE = os.path.expanduser("~/.previewapk/credentials.json")
SETUP_FILE = os.path.expanduser("~/.previewapk/setup.json")


def _saved_key():
    """The key `previewapk setup` saved on this machine, if any."""
    try:
        with open(CRED_FILE) as f:
            return json.load(f).get("api_key", "")
    except Exception:
        return ""


# Env wins; the saved credential is the fallback (one `setup` covers
# both clients — the MCP server reads the same file).
KEY = os.environ.get("PREVIEWAPK_API_KEY", "")
KEY_FROM_FILE = False
if not KEY:
    KEY = _saved_key()
    KEY_FROM_FILE = bool(KEY)


def fail(msg):
    print(f"previewapk: {msg}", file=sys.stderr)
    sys.exit(1)


def _write_json_private(path, obj):
    """Write a JSON file mode 0600 (credentials live in these)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def _setup_post(path, payload):
    """POST to the (unauthenticated) setup endpoints. Returns
    (status, json) — errors are data here, not exceptions."""
    data = json.dumps(payload).encode()
    r = urllib.request.Request(HOST + path, data=data, method="POST",
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}
    except Exception as e:
        raise TransportError(str(e))


def _state_with(key):
    r = urllib.request.Request(HOST + "/portal/api/state",
                               headers={"X-Api-Key": key})
    with urllib.request.urlopen(r, timeout=30) as resp:
        return json.loads(resp.read() or b"{}")


def cmd_setup(a):
    """Agent-led setup (see https://previewapk.siteport.site/setup.md):
    one JSON line per step on stdout. The key is saved to
    ~/.previewapk/credentials.json (0600) and NEVER printed."""
    def line(obj):
        print(json.dumps(obj))

    # Step 0: a working key already? Setup is done — never mint a
    # second key over a working one.
    if KEY:
        try:
            st = _state_with(KEY)
        except urllib.error.HTTPError as e:
            st = None
            if e.code != 401:
                line({"step": "error",
                      "error": f"the server answered HTTP {e.code} while checking the existing key — try again in a moment"})
                sys.exit(1)
        except Exception as e:
            line({"step": "error",
                  "error": f"could not reach PreviewAPK to check the existing key ({e}) — try again in a moment"})
            sys.exit(1)
        if st is not None:
            devs = [d.get("name") for d in st.get("devices", [])]
            if devs:
                line({"step": "ready", "account": st.get("email"),
                      "devices": devs,
                      "note": "already set up — the existing key works; nothing was minted"})
            else:
                line({"step": "no_slots", "account": st.get("email"),
                      "next": "portal",
                      "note": "signed in, but this account has no device slot yet"})
            return
        if not KEY_FROM_FILE:
            line({"step": "error",
                  "error": "PREVIEWAPK_API_KEY is set but the server rejected it — fix it or unset it, then run setup again"})
            sys.exit(1)
        # A saved key the server rejects (revoked/rotated): fall through
        # to a fresh grant — approving it overwrites the dead key.

    # Resume a pending grant from an earlier run when it is still alive.
    pending = None
    try:
        with open(SETUP_FILE) as f:
            pending = json.load(f)
    except Exception:
        pass
    code = (pending or {}).get("code")
    if not code or (pending or {}).get("expires_at", 0) < time.time():
        try:
            status, r = _setup_post("/portal/api/setup/start",
                                    {"label": getattr(a, "label", "") or ""})
        except TransportError as e:
            line({"step": "error", "error": f"could not reach PreviewAPK ({e})"})
            sys.exit(1)
        if status != 200 or not r.get("code"):
            line({"step": "error",
                  "error": r.get("error", f"setup start failed (HTTP {status})"),
                  "next": r.get("next", "try again in a moment")})
            sys.exit(1)
        code, url = r["code"], r.get("url")
        expires_in = int(r.get("expires_in", 600))
        _write_json_private(SETUP_FILE, {"code": code, "url": url,
                                         "expires_at": time.time() + expires_in})
    else:
        url = pending.get("url")
        expires_in = max(0, int(pending["expires_at"] - time.time()))
    line({"step": "sign_in", "code": code, "url": url, "expires_in": expires_in})
    if a.no_wait:
        return
    deadline = time.time() + expires_in + 30
    while time.time() < deadline:
        time.sleep(3)
        try:
            status, r = _setup_post("/portal/api/setup/token", {"code": code})
        except TransportError:
            continue
        st = r.get("status")
        if st == "pending":
            continue
        try:
            os.unlink(SETUP_FILE)
        except Exception:
            pass
        if st == "approved" and r.get("key"):
            _write_json_private(CRED_FILE, {"api_key": r["key"],
                                            "email": r.get("email"),
                                            "saved_at": time.time()})
            devs = r.get("devices") or []
            if devs:
                line({"step": "ready", "account": r.get("email"),
                      "key_saved": "~/.previewapk/credentials.json",
                      "devices": devs})
            else:
                line({"step": "no_slots", "account": r.get("email"),
                      "key_saved": "~/.previewapk/credentials.json",
                      "next": "portal"})
            return
        if st == "denied":
            line({"step": "denied",
                  "next": "setup needs the account owner's approval — or copy a key manually from the portal's Developers card"})
            sys.exit(1)
        line({"step": "expired",
              "next": "run `previewapk setup` again for a fresh code"})
        sys.exit(1)
    line({"step": "expired", "next": "run `previewapk setup` again for a fresh code"})
    sys.exit(1)


class TransportError(Exception):
    """The request or its response died in transit. The server may or
    may not have acted — callers must verify state, never assume."""


def envelope_message(body, status):
    """Render an API error body for a human: the plain error, plus the
    Phase-1 envelope when present — the stable [code] to branch on,
    the concrete next step, the request id for support, and any
    candidate elements an ambiguous selector matched."""
    try:
        j = json.loads(body)
        err = j.get("error", body.decode("utf-8", "replace"))
    except Exception:
        return body.decode("utf-8", "replace")
    if not isinstance(j, dict):
        return str(err)
    msg = f"HTTP {status}: {err}"
    if j.get("code"):
        msg += f" [{j['code']}]"
    if j.get("next"):
        msg += f"\n  next: {j['next']}"
    if j.get("candidates"):
        msg += "\n  candidates:"
        for i, c in enumerate(j["candidates"]):
            if isinstance(c, dict):
                lbl = c.get("text") or c.get("desc") or ""
                msg += (f"\n    {i}: {c.get('type', '?')} {lbl!r}"
                        f" #{c.get('id', '')} @({', '.join(str(v) for v in c.get('center', []))})")
    if j.get("request_id"):
        msg += f"\n  (request {j['request_id']})"
    return msg


def _req_once(method, path, payload, timeout, raw):
    data = None
    headers = {"X-Api-Key": KEY}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(HOST + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        fail(envelope_message(e.read(), e.code))
    except Exception as e:
        raise TransportError(str(e))
    return body if raw else json.loads(body or b"{}")


def req(method, path, payload=None, timeout=60, raw=False, raise_transport=False):
    if not KEY:
        fail("no API key — run `previewapk setup`, or set PREVIEWAPK_API_KEY (key in your portal's Developers card)")
    # Reads are safe to retry when a connection drops; actions are not
    # retried blind (a tap could land twice) — they verify instead.
    # Failures arrive in bursts (a bad few seconds on the path), so the
    # last read retry waits longer to clear the window.
    attempts = 4 if method == "GET" else 1
    for i in range(attempts):
        try:
            return _req_once(method, path, payload, timeout, raw)
        except TransportError as e:
            if i + 1 < attempts:
                time.sleep(min(0.7 * (2 ** i), 2.5))
                continue
            if raise_transport:
                raise
            fail(f"request failed: {e}")


def upload_apk(path, device, replace=False):
    if not KEY:
        fail("no API key — run `previewapk setup`, or set PREVIEWAPK_API_KEY")
    boundary = "----pvk" + uuid.uuid4().hex
    with open(path, "rb") as f:
        raw = f.read()
    name = os.path.basename(path)
    parts = []
    if device:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="device"\r\n\r\n{device}\r\n'.encode())
    if replace:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="replace"\r\n\r\n1\r\n'.encode())
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="apk"; filename="{name}"\r\n'
                  f'Content-Type: application/vnd.android.package-archive\r\n\r\n').encode())
    parts.append(raw)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)
    r = urllib.request.Request(HOST + "/portal/api/upload", data=body, method="POST",
                               headers={"X-Api-Key": KEY,
                                        "Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(r, timeout=300) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        fail(envelope_message(e.read(), e.code))
    except Exception as e:
        raise TransportError(str(e))


STATE_FILE = os.path.expanduser("~/.previewapk/current.json")


def load_selection():
    try:
        with open(STATE_FILE) as f:
            return json.load(f).get("device_id", "")
    except Exception:
        return ""


def save_selection(d):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump({"device_id": d["id"], "device_name": d.get("name", d["id"])}, f)
    except Exception:
        pass


def get_state(raise_transport=False):
    return req("GET", "/portal/api/state", raise_transport=raise_transport)


def _ver(s):
    try:
        return tuple(int(x) for x in str(s).split("."))
    except Exception:
        return ()


def upgrade_notice(state, kind):
    """One plain line when the server knows a newer client than this
    one. Silent when the server doesn't say (old server, no field)."""
    info = ((state or {}).get("clients") or {}).get(kind) or {}
    latest = info.get("version")
    if latest and _ver(latest) > _ver(VERSION):
        return (f"previewapk {kind} {latest} is available (you're running {VERSION}): "
                f"curl -o {info.get('file', 'previewapk.py')} {info.get('url', '')}")
    return ""


def skill_notice(state):
    """One factual line naming the current published skill version,
    from live state. The CLI cannot know which skill copy the agent
    loaded, so this never claims staleness — the agent (and the
    skill's own self-refresh section) does the comparing. Silent
    when the server doesn't advertise a skill (old server)."""
    info = ((state or {}).get("clients") or {}).get("skill") or {}
    ver, url = info.get("version"), info.get("url")
    if ver and url:
        return (f"Skill file: the current PreviewAPK skill is v{ver} at {url}; "
                f"if your loaded skill is older, re-fetch it.")
    return ""


def pick_device(state, want):
    devs = state.get("devices", [])
    if not devs:
        fail("no devices on this account")
    if want:
        for d in devs:
            if d["id"] == want:
                return d
        for d in devs:
            if (d.get("name") or "").lower() == want.lower():
                return d
        fail(f"no device '{want}' (have: {', '.join(d.get('name', d['id']) for d in devs)})")
    fail("no working device — select one with `previewapk use <name>` (list: `previewapk devices`) or pass --device. Nothing is guessed: you must state where you are working.")


def dev_action(device, action, payload=None, timeout=60):
    return req("POST", f"/portal/api/device/{device}/{action}", payload or {},
               timeout=timeout, raise_transport=True)


def get_observe(device):
    return dev_action(device, "input", {"action": "observe"}, timeout=90)


def screen_digest(device):
    """A hash of what's on screen (structure + text), or None when the
    hierarchy is unavailable. The hierarchy channel is small and the
    most reliable read we have — it is the receipt's witness."""
    try:
        r = req("GET", f"/portal/api/device/{device}/hierarchy",
                timeout=30, raise_transport=True)
    except TransportError:
        return None
    tree = r.get("tree")
    if not tree:
        return None
    h = hashlib.sha256()

    def walk(n):
        h.update(((n.get("class") or "") + "|" + (n.get("text") or "") + "|"
                  + (n.get("desc") or "") + "|" + str(n.get("bounds")) + ";").encode())
        for c in n.get("children", []):
            walk(c)
    walk(tree)
    return h.hexdigest()


def run_action(device, action, payload=None, kind="input", want_driver=None):
    """Run a mutating action and return (result, receipt).

    receipt is "confirmed" (the server answered), "landed" (the
    connection died but a state check proves the action took effect),
    or "unknown" (connection died and state cannot settle it — the
    caller reports that honestly instead of guessing). When the state
    check proves the action did NOT land, it is retried once before
    giving up — a dead action leaves no trace, so the retry is safe.
    """
    before = screen_digest(device) if kind == "input" else None
    try:
        return dev_action(device, action, payload), "confirmed"
    except TransportError:
        pass
    time.sleep(0.9)
    if kind == "input":
        after = screen_digest(device)
        if before is not None and after is not None:
            if after != before:
                return {"landed": True, "verified_by": "screen change"}, "landed"
            try:
                return dev_action(device, action, payload), "confirmed"
            except TransportError:
                return {}, "unknown"
        return {}, "unknown"
    # session-level actions: verify against the device list state
    try:
        st = get_state(raise_transport=True)
    except TransportError:
        return {}, "unknown"
    dv = next((x for x in st.get("devices", []) if x["id"] == device), None)
    if dv is None:
        return {}, "unknown"
    if action == "stop" and dv.get("status") == "off":
        return {"landed": True, "verified_by": "device state"}, "landed"
    if action == "run" and dv.get("status") in ("live", "starting"):
        return {"landed": True, "verified_by": "device state"}, "landed"
    if action == "handoff" and want_driver and dv.get("driver") == want_driver:
        return {"landed": True, "verified_by": "device state"}, "landed"
    try:
        return dev_action(device, action, payload), "confirmed"
    except TransportError:
        return {}, "unknown"


def fetch_screenshot(device, scale=1.0):
    """Download a screenshot, resuming from the byte a cut transfer
    reached. Bulk responses die mid-stream on some networks agents run
    behind (agent QA, 2026-10-22); with Range + the portal's frame
    cache, a cut is a hiccup, not a failure. Returns (png_bytes, tries).
    """
    path = f"/portal/api/device/{device}/screenshot"
    if scale and scale != 1.0:
        path += f"?scale={scale}"
    buf = bytearray()
    total = None
    cap = None
    tries = 0
    for _ in range(24):
        tries += 1
        headers = {"X-Api-Key": KEY}
        if buf:
            headers["Range"] = f"bytes={len(buf)}-"
        r = urllib.request.Request(HOST + path, headers=headers)
        status, hdrs, chunk = 0, {}, b""
        try:
            with urllib.request.urlopen(r, timeout=90) as resp:
                status, hdrs = resp.status, resp.headers
                try:
                    chunk = resp.read()
                except http.client.IncompleteRead as e:
                    chunk = e.partial or b""
        except urllib.error.HTTPError as e:
            if e.code in (503, 416):  # no frame right now / frame moved on
                buf, total, cap = bytearray(), None, None
                time.sleep(1.5)
                continue
            fail(f"HTTP {e.code}: screenshot failed")
        except Exception:
            time.sleep(0.5)  # connection died before/while answering
            continue
        if status == 200 and buf:
            buf, total, cap = bytearray(), None, None  # Range ignored; restart
        if status in (200, 206):
            this_cap = hdrs.get("X-Captured-At")
            if cap is not None and this_cap and this_cap != cap:
                buf, total, cap = bytearray(), None, None
                continue  # the frame changed mid-assembly; start over
            cap = cap or this_cap
            cr = hdrs.get("Content-Range") or ""
            if "/" in cr:
                try:
                    total = int(cr.rsplit("/", 1)[1])
                except Exception:
                    pass
            if total is None and status == 200:
                try:
                    total = int(hdrs.get("Content-Length") or 0) or None
                except Exception:
                    pass
            if chunk:
                if status == 206 and cr:
                    try:
                        start = int(cr.split(" ", 1)[1].split("-", 1)[0])
                    except Exception:
                        start = len(buf)
                    if start != len(buf):
                        continue  # misaligned slice; re-request
                buf.extend(chunk)
        if total is not None and len(buf) >= total:
            break
        time.sleep(0.3)
    data = bytes(buf)
    if not data.startswith(b"\x89PNG") or b"IEND" not in data[-12:]:
        fail("screenshot transfer could not be completed — the network kept "
             "cutting it. Try `shot --scale 0.5` (a smaller frame survives "
             "networks that mangle large responses).")
    return data, tries


def fetch_binary(path):
    """Resumable download of a static payload (a recording MP4, a
    pulled device file) — the same Range discipline as
    fetch_screenshot, without the PNG frame checks. Returns bytes."""
    buf = bytearray()
    total = None
    for _ in range(64):
        headers = {"X-Api-Key": KEY}
        if buf:
            headers["Range"] = f"bytes={len(buf)}-"
        r = urllib.request.Request(HOST + path, headers=headers)
        status, hdrs, chunk = 0, {}, b""
        try:
            with urllib.request.urlopen(r, timeout=120) as resp:
                status, hdrs = resp.status, resp.headers
                try:
                    chunk = resp.read()
                except http.client.IncompleteRead as e:
                    chunk = e.partial or b""
        except urllib.error.HTTPError as e:
            fail(envelope_message(e.read(), e.code))
        except Exception:
            time.sleep(0.5)  # connection died before/while answering
            continue
        if status == 200 and buf:
            buf, total = bytearray(), None  # Range ignored; restart
        if status in (200, 206):
            cr = hdrs.get("Content-Range") or ""
            if "/" in cr:
                try:
                    total = int(cr.rsplit("/", 1)[1])
                except Exception:
                    pass
            if total is None and status == 200:
                try:
                    total = int(hdrs.get("Content-Length") or 0) or None
                except Exception:
                    pass
            if chunk:
                if status == 206 and cr:
                    try:
                        start = int(cr.split(" ", 1)[1].split("-", 1)[0])
                    except Exception:
                        start = len(buf)
                    if start != len(buf):
                        continue  # misaligned slice; re-request
                buf.extend(chunk)
        if total is not None and len(buf) >= total:
            break
        time.sleep(0.3)
    if total is None or len(buf) < total:
        fail("the download could not be completed — the network kept "
             "cutting it. Run it again; transfers resume where they stopped.")
    return bytes(buf)


def get_hierarchy(device):
    return req("GET", f"/portal/api/device/{device}/hierarchy", timeout=90)


def get_logs(device, params):
    import urllib.parse as _up
    qs = _up.urlencode({k: v for k, v in params.items() if v is not None})
    return req("GET", f"/portal/api/device/{device}/logs" + (f"?{qs}" if qs else ""), timeout=60)


def flat_nodes(tree):
    """Flatten a hierarchy tree into a list of element dicts."""
    out = []

    def walk(n):
        if not n:
            return
        out.append(n)
        for c in n.get("children", []):
            walk(c)
    walk(tree)
    return out


def fmt_node(n):
    bits = [n.get("class", "?")]
    if n.get("text"):
        bits.append(f'"{n["text"]}"')
    if n.get("desc"):
        bits.append(f'desc="{n["desc"]}"')
    if n.get("id"):
        bits.append(f'#{n["id"]}')
    if "center" in n:
        bits.append(f'@({n["center"][0]}, {n["center"][1]})')
    flags = [f for f in ("click", "long", "scroll", "edit", "check", "selected", "focused") if n.get(f)]
    if n.get("check"):
        flags.append("checked" if n.get("checked") else "unchecked")
    if n.get("enabled") is False:
        flags.append("disabled")
    if flags:
        bits.append("[" + ",".join(flags) + "]")
    return " ".join(bits)


def print_tree(tree, depth=0, out_lines=None):
    if out_lines is None:
        out_lines = []
    if not tree:
        return out_lines
    out_lines.append("  " * depth + fmt_node(tree))
    for c in tree.get("children", []):
        print_tree(c, depth + 1, out_lines)
    return out_lines


def find_matches(tree, text=None, desc=None, rid=None):
    nodes = flat_nodes(tree)
    if text:
        # Labels live in `text` on native views and in `desc` on
        # Flutter/semantics-driven UIs — search both.
        for field in ("text", "desc"):
            exact = [n for n in nodes if n.get(field) == text]
            if exact:
                return exact
        return [n for n in nodes if text.lower() in (n.get("text") or "").lower()
                or text.lower() in (n.get("desc") or "").lower()]
    if desc:
        exact = [n for n in nodes if n.get("desc") == desc]
        if exact:
            return exact
        return [n for n in nodes if desc.lower() in (n.get("desc") or "").lower()]
    if rid:
        return [n for n in nodes if n.get("id") == rid or (n.get("id") or "").endswith("/" + rid)]
    return []


def main():
    common = argparse.ArgumentParser(add_help=False)
    # SUPPRESS defaults: with the same parent on the main parser and every
    # subparser, a plain default on the subparser copy would clobber a
    # value given before the subcommand. Set only when actually present.
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="machine-readable output")
    common.add_argument("--device", default=argparse.SUPPRESS,
                        help="device name or id (required; or set PREVIEWAPK_DEVICE to pin one)")
    ap = argparse.ArgumentParser(prog="previewapk", add_help=True, parents=[common])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("push", parents=[common], help="push an APK to the device (attach it; --run starts a session)"); p.add_argument("apk"); p.add_argument("--run", action="store_true"); p.add_argument("--replace", action="store_true", help="on a live device, allow replacing a NEWER installed build with this older one (uninstalls it first — its data is deleted)")
    p.add_argument("--driver", choices=["agent", "user"], default="agent")
    p = sub.add_parser("start", parents=[common], help="start the device's attached app"); p.add_argument("--driver", choices=["agent", "user"], default="agent")
    sub.add_parser("stop", parents=[common], help="stop the device (the session link dies with the session)"); sub.add_parser("status", parents=[common], help="device + session state"); sub.add_parser("links", parents=[common], help="the share link for the live session")
    sub.add_parser("relaunch", parents=[common], help="bring the app back to the foreground (no reboot)")
    sub.add_parser("devices", parents=[common], help="list the devices on this account")
    p = sub.add_parser("use", parents=[common], help="select the working device"); p.add_argument("name", nargs="?")
    p = sub.add_parser("tap", parents=[common], help="tap by X Y fractions (0..1), or --text/--desc/--id/--field to tap an element"); p.add_argument("x", type=float, nargs="?"); p.add_argument("y", type=float, nargs="?")
    p.add_argument("--text"); p.add_argument("--desc"); p.add_argument("--id", dest="rid"); p.add_argument("--index", type=int, default=None)
    p.add_argument("--field", type=int, help="tap the Nth input field on screen (1-based)")
    p.add_argument("--ref", type=int, help="tap ref N from a `look` snapshot (needs --snapshot)")
    p.add_argument("--snapshot", help="snapshot_id printed by `look`")
    sub.add_parser("look", parents=[common], help="compact numbered snapshot of the screen: refs you can tap with tap --ref")
    p = sub.add_parser("wait", parents=[common], help="wait until an element appears (or --gone: disappears), or --for driver: until the human hands the wheel back; exit 3 on timeout")
    p.add_argument("--for", dest="wait_for", choices=["element", "driver"], default="element", help="what to wait for: an element (default) or the handback (driver)")
    p.add_argument("--text"); p.add_argument("--desc"); p.add_argument("--id", dest="rid")
    p.add_argument("--gone", action="store_true", help="wait until the element is GONE instead of present")
    p.add_argument("--timeout", type=float, default=None, help="seconds to wait (default 30 for elements, 300 for --for driver; element waits max 120)")
    p = sub.add_parser("act", parents=[common], help="run a batch of actions from a JSON file (or stdin): {\"actions\": [...], \"observe\": \"none|ui|screenshot|both\"}")
    p.add_argument("file", nargs="?", default="-", help="batch JSON file, or - for stdin")
    p.add_argument("--shot-out", default="previewapk-act.png", help="where a post-batch screenshot (observe=screenshot|both) is saved")
    sub.add_parser("tree", parents=[common], help="the UI element tree: text, ids, tap coordinates — verify the screen without a screenshot")
    p = sub.add_parser("logs", parents=[common], help="the app's logcat lines for this session"); p.add_argument("--since", type=float); p.add_argument("--lines", type=int, default=200)
    p.add_argument("--level", choices=["V", "D", "I", "W", "E", "F"]); p.add_argument("--errors", action="store_true")
    p.add_argument("--grep", dest="q"); p.add_argument("--follow", action="store_true")
    p = sub.add_parser("swipe", parents=[common], help="swipe between two points (0..1 fractions)"); p.add_argument("x1", type=float); p.add_argument("y1", type=float)
    p.add_argument("x2", type=float); p.add_argument("y2", type=float); p.add_argument("ms", type=int, nargs="?", default=300)
    p = sub.add_parser("key", parents=[common], help="send an Android keycode (4=Back, 3=Home, 66=Enter)"); p.add_argument("code", type=int)
    p = sub.add_parser("text", parents=[common], help="type text into the focused field"); p.add_argument("text")
    p = sub.add_parser("shot", parents=[common], help="save a screenshot (download resumes if the connection is cut)"); p.add_argument("out", nargs="?", default="previewapk-shot.png"); p.add_argument("--scale", type=float, default=1.0, help="0.5 = half-size frame, ~4x smaller transfer for weak networks")
    p = sub.add_parser("handoff", parents=[common], help="pass the wheel to the other driver mid-session"); p.add_argument("driver", choices=["agent", "user"])
    p = sub.add_parser("request-human", parents=[common], help="ask the human for a step only a human should do (password, 2FA, payment, CAPTCHA) — prints the exact message to relay"); p.add_argument("reason", help="one short line: what the human needs to do")
    p = sub.add_parser("finish", parents=[common], help="file the session wrap-up (counts come from the server) and print the exact message to relay to the human"); p.add_argument("--summary", required=True, help="what you tested or checked, in your own words"); p.add_argument("--verdict", choices=["passed", "failed", "mixed"], help="optional overall verdict"); p.add_argument("--end-session", action="store_true", help="also stop the session after filing (default: it keeps running)")
    p = sub.add_parser("record", parents=[common], help="screen recording: start | stop | status | pull <file>")
    rs = p.add_subparsers(dest="record_cmd", required=True)
    rp = rs.add_parser("start", parents=[common], help="start recording the screen (max 180s per recording)"); rp.add_argument("--name", help="a label for the recording")
    rs.add_parser("stop", parents=[common], help="stop the recording and pull the MP4 to the server")
    rs.add_parser("status", parents=[common], help="is a recording running? what was the last one?")
    rp = rs.add_parser("pull", parents=[common], help="download a finished recording (default: the latest)"); rp.add_argument("out", nargs="?", default="previewapk-recording.mp4"); rp.add_argument("--id", dest="rec_id", help="recording id from record stop/status")
    p = sub.add_parser("device", parents=[common], help="device controls: timezone | locale | location | location-clear | push | pull | ls | rm | reboot")
    ds = p.add_subparsers(dest="device_cmd", required=True)
    dp = ds.add_parser("timezone", parents=[common], help="set the device timezone (IANA name)"); dp.add_argument("tz")
    dp = ds.add_parser("locale", parents=[common], help="set the session app's language (BCP 47 tag, per-app)"); dp.add_argument("locale"); dp.add_argument("--package", help="package to set (default: the session's app)")
    dp = ds.add_parser("location", parents=[common], help="make GPS report a mock fix"); dp.add_argument("lat", type=float); dp.add_argument("lon", type=float)
    ds.add_parser("location-clear", parents=[common], help="remove the mock GPS fix")
    dp = ds.add_parser("push", parents=[common], help="push a local file (max 10 MB) into the device's shared storage"); dp.add_argument("local"); dp.add_argument("remote", nargs="?", help="device path under /sdcard (default: Download/<name>)")
    dp = ds.add_parser("pull", parents=[common], help="pull a file (max 10 MB) from the device's shared storage"); dp.add_argument("remote"); dp.add_argument("out", nargs="?", help="local path (default: the file's name)")
    dp = ds.add_parser("ls", parents=[common], help="list a folder in shared storage"); dp.add_argument("path", nargs="?", default="Download")
    dp = ds.add_parser("rm", parents=[common], help="delete a file from shared storage"); dp.add_argument("path")
    ds.add_parser("reboot", parents=[common], help="restart the phone (same phone, same data; takes under a minute)")
    p = sub.add_parser("setup", parents=[common], help="agent-led setup: approve a short code in the portal; the key is minted there and saved here (never printed)")
    p.add_argument("--no-wait", action="store_true", help="print the sign_in step JSON and exit; re-run setup to poll")
    p.add_argument("--label", help="how this setup identifies itself on the approval page (e.g. 'Claude Code on laptop')")
    p = sub.add_parser("feedback", parents=[common], help="file a field report: what broke, what worked, what you wish the tool had")
    p.add_argument("--worked"); p.add_argument("--broke"); p.add_argument("--wish")
    p.add_argument("--workaround"); p.add_argument("--rating", type=int, choices=[1, 2, 3, 4, 5])
    a = ap.parse_args()
    if a.cmd == "setup":
        return cmd_setup(a)
    a.json = getattr(a, "json", False)
    a.device = getattr(a, "device", None) or load_selection() or os.environ.get("PREVIEWAPK_DEVICE", "")

    def out(obj, human):
        print(json.dumps(obj, indent=1) if a.json else human)

    def act(device, action, payload=None, kind="input", want_driver=None):
        """Run an action with a receipt. Returns (result, note) where
        note is empty when the server confirmed, or explains the
        verification when the connection died but the action landed.
        An unsettleable outcome is reported as unknown (exit 2) —
        never dressed up as success or failure."""
        res, rc = run_action(device, action, payload, kind=kind,
                             want_driver=want_driver)
        if rc == "unknown":
            out({"landed": None, "action": action},
                "The connection dropped mid-call and the state check could "
                "not settle whether it landed. Verify with `previewapk tree` "
                "or `previewapk status` before repeating it.")
            sys.exit(2)
        note = "" if rc == "confirmed" else " (connection dropped mid-call — verified landed)"
        return res, note

    def links_of(device):
        try:
            return dev_action(device, "links")
        except TransportError:
            return {}

    if a.cmd == "push":
        d0 = pick_device(get_state(), a.device)
        save_selection(d0)
        try:
            res = upload_apk(a.apk, d0["id"], replace=a.replace)
        except TransportError:
            try:
                st = get_state(raise_transport=True)
            except TransportError:
                st = {}
            dv = next((x for x in st.get("devices", []) if x["id"] == d0["id"]), {})
            app = (dv.get("apk") or {}).get("label")
            out({"landed": None, "device": d0["id"], "attached_app": app},
                f"The connection dropped during the push, and a dropped "
                f"upload cannot be confirmed from here. The device now "
                f"shows: {dv.get('name', d0['id'])} — {dv.get('status')} — "
                f"attached app: {app or 'none'}. If that is the build you "
                f"pushed, it landed; otherwise push again.")
            sys.exit(2)
        dev_id = res.get("device_id") or d0["id"]
        human = (f"Pushed {res.get('app', {}).get('label', a.apk)} "
                 f"v{res.get('app', {}).get('version_name', '?')} to device {dev_id}.")
        if res.get("swapped"):
            human += (" The device was running — the new app was swapped in "
                      "live and relaunched, no reboot.")
        if res.get("deduped"):
            human += (" Identical bytes were already stored on the account"
                      " — reused, not stored twice.")
        if res.get("replaced"):
            human += (" The newer installed build was uninstalled first,"
                      " as requested — its data was deleted.")
        if a.run and not res.get("swapped"):
            r = dev_action(dev_id, "run", {"driver": a.driver})
            links = links_of(dev_id)
            human += (f"\nSession starting (driver: {a.driver})."
                      f"\nShare link: run `previewapk links` once it's live.")
            res = {**res, "run": r, "links": links}
        out(res, human)
        return

    state = get_state()
    if a.cmd == "use":
        if a.name:
            du = pick_device(state, a.name)
            save_selection(du)
            app = f"{du['apk']['label']}" if du.get("apk") else "no app attached"
            out({"selected": du.get("name"), "id": du["id"]},
                f"Working device: {du.get('name')} ({du['id']}) — {du.get('profile')}, Android {du.get('android')} — {du['status']} — {app}\nEvery command now talks to this device until you `use` another.")
        else:
            sel = load_selection()
            cur = next((dv for dv in state.get("devices", []) if dv["id"] == sel), None)
            out({"selected": cur.get("name") if cur else None},
                f"Working device: {cur.get('name')} ({cur['id']}) — {cur['status']}" if cur else "No working device selected. List with `previewapk devices`, select with `previewapk use <name>`.")
        return
    if a.cmd == "devices":
        rows = []
        for dv in state.get("devices", []):
            app = f"{dv['apk']['label']} v{dv['apk'].get('version_name')}" if dv.get("apk") else "no app attached"
            mark = " <- working device" if dv["id"] == load_selection() else ""
            rows.append(f"{dv.get('name', dv['id'])} ({dv['id']}) — {dv.get('profile')}, Android {dv.get('android')} — {dv['status']} — {app}{mark}")
        out(state.get("devices", []), "\n".join(rows) or "No devices on this account.")
        return
    if a.cmd == "feedback":
        rep = {k: getattr(a, k) for k in ("worked", "broke", "wish", "workaround")
               if getattr(a, k)}
        if not rep:
            fail("a field report needs at least one of --worked, --broke, --wish, --workaround")
        if state.get("share_diagnostics") is False:
            fail("diagnostic sharing is turned off for this account — the owner opted out in the portal's Privacy settings, so no report was sent")
        if a.rating:
            rep["rating"] = a.rating
        rep["client"] = "cli"
        rep["client_version"] = VERSION
        sel = load_selection()
        if sel:
            rep["device"] = sel
        r = req("POST", "/portal/api/feedback", rep)
        out(r, r.get("message", "Field report filed."))
        return
    d = pick_device(state, a.device)
    did = d["id"]

    if a.cmd == "start":
        save_selection(d)
        r, note = act(did, "run", {"driver": a.driver}, kind="session")
        links = links_of(did)
        un = upgrade_notice(state, "cli")
        if un:
            r["client_notice"] = un
        out({**r, "links": links},
            f"Session starting on {d['name']} (driver: {a.driver}){note}.\nShare link: run `previewapk links` once it's live."
            + (f"\n{un}" if un else ""))
    elif a.cmd == "stop":
        r, note = act(did, "stop", kind="session")
        inv = ("" if state.get("share_diagnostics") is False else
               "\nYou drove it — file a field report while it's fresh: previewapk feedback --broke \"...\" --wish \"...\" (what worked and what you had to work around count too). Reports are read and tallied; they decide what gets fixed and built next.")
        out(r, f"{d['name']} stopped{note}. The session link died with the session.{inv}")
    elif a.cmd == "status":
        un = upgrade_notice(state, "cli")
        sn = skill_notice(state)
        human = (f"{d['name']} ({did}) — {d['status']}"
                 + (f", driven by {d.get('driver')}" if d["status"] == "live" else "")
                 + (f", app: {d['apk']['label']} v{d['apk'].get('version_name')}" if d.get("apk") else ", no app attached")
                 + (f"\nSession link: {d.get('view_url')}" if d.get("view_url") else "")
                 + (f"\n{un}" if un else "")
                 + (f"\n{sn}" if sn else ""))
        if d.get("handoff"):
            human += f"\nAgent needs the human: {d['handoff'].get('reason')}"
        if d.get("resumed"):
            human += (f"\nWheel handed back — human's note: "
                      f"{d.get('handback_note') or '(none)'}")
        fr = d.get("finish_report")
        if fr:
            human += (f"\nWrap-up filed: {fr.get('summary')}"
                      + (f" [{fr['verdict']}]" if fr.get("verdict") else ""))
        extra = {}
        if un:
            extra["client_notice"] = un
        if sn:
            extra["skill_notice"] = sn
        out({**d, **extra} if extra else d, human)
    elif a.cmd == "links":
        r = req("POST", f"/portal/api/device/{did}/links", {})
        out(r, f"Session link (view-only, dies with the session): {r.get('session_url') or '— device is off'}\n"
               f"Driver: {r.get('driver')}" + (f" — {r.get('driver_note')}" if r.get('driver_note') else ""))
    elif a.cmd == "logs":
        params = {"lines": a.lines, "since": a.since, "q": a.q,
                  "level": "E" if a.errors else a.level}
        if a.follow:
            after = None
            print("Following logs — Ctrl-C to stop.", file=sys.stderr)
            try:
                while True:
                    r = get_logs(did, {**params, "after": after, "since": None} if after is not None else params)
                    for e in r.get("lines", []):
                        if a.errors and e.get("env"):
                            continue  # emulator noise stays out of --errors
                        print(e["raw"], flush=True)
                    if r.get("latest") is not None:
                        after = r["latest"]
                    if r.get("ended"):
                        print("(session ended — showing the saved tail)", file=sys.stderr)
                        break
                    time.sleep(2)
            except KeyboardInterrupt:
                pass
            return
        r = get_logs(did, params)
        lines = [e["raw"] for e in r.get("lines", [])
                 if not (a.errors and e.get("env"))]
        note = " (session ended — saved tail of the last session)" if r.get("ended") else ""
        out(r, "\n".join(lines) + (f"\n[{r.get('count', len(lines))} line(s){note}]" if lines else f"No log lines matched.{note}"))
    elif a.cmd == "tree":
        r = get_hierarchy(did)
        lines = print_tree(r.get("tree"))
        out(r, "\n".join(lines) if lines else "(empty hierarchy)")
    elif a.cmd == "look":
        r = get_observe(did)
        out(r, r.get("text") or "(empty screen)")
    elif a.cmd == "tap":
        if a.ref is not None:
            if not a.snapshot:
                fail("tap --ref needs --snapshot <snapshot_id> — the id `look` printed at the top of its snapshot")
            r2, note = act(did, "input", {"action": "tap_ref",
                                          "snapshot_id": a.snapshot,
                                          "ref": a.ref})
            t = (r2.get("tapped") or {})
            lbl = t.get("text") or t.get("desc") or t.get("type") or "element"
            out(r2, f"Tapped ref {a.ref} ({lbl}){note}.")
        elif a.field is not None:
            r = get_hierarchy(did)
            fields = [n for n in flat_nodes(r.get("tree"))
                      if n.get("edit") or "EditText" in (n.get("class") or "")]
            if not fields:
                fail("no input fields on the current screen")
            if not (1 <= a.field <= len(fields)):
                fail(f"only {len(fields)} input field(s) on this screen (asked for #{a.field})")
            n = fields[a.field - 1]
            if "center" not in n:
                fail("that field has no bounds to tap")
            r2, note = act(did, "input", {"action": "tap", "x": n["center"][0], "y": n["center"][1]})
            out(r2, f"Tapped input field #{a.field} ({fmt_node(n)}){note}.")
        elif a.text or a.desc or a.rid:
            r = get_hierarchy(did)
            ms = find_matches(r.get("tree"), a.text, a.desc, a.rid)
            if not ms:
                nodes = flat_nodes(r.get("tree"))
                labels = []
                for n in nodes:
                    t = n.get("text") or n.get("desc")
                    if t and t not in labels:
                        labels.append(t)
                nf = len([n for n in nodes
                          if n.get("edit") or "EditText" in (n.get("class") or "")])
                hint = ""
                if labels:
                    hint += " Visible labels: " + ", ".join(repr(x) for x in labels[:12]) + "."
                if nf:
                    hint += (f" {nf} input field(s) on screen — tap one with "
                             f"`tap --field N` (form-field hint text is not "
                             f"exposed by the hierarchy).")
                fail("no element matches that selector on the current screen." + hint)
            if len(ms) > 1 and a.index is None:
                # Ambiguous at the same match level: list the candidates
                # and stop — never silently pick one (Phase 1, 2026-10-23).
                cands = [{"type": n.get("class"), "text": n.get("text"),
                          "desc": n.get("desc"), "id": n.get("id"),
                          "center": n.get("center")} for n in ms]
                human = (f"{len(ms)} elements match that selector — refusing to guess. "
                         f"Re-run with --index N:\n" + "\n".join(
                             f"  {i}: {fmt_node(n)}" for i, n in enumerate(ms)))
                out({"error": f"{len(ms)} elements match that selector",
                     "code": "ambiguous_target", "retryable": False,
                     "next": "re-run with --index N (0-based, in the order listed)",
                     "candidates": cands}, human)
                sys.exit(1)
            idx = a.index if a.index is not None else 0
            if idx >= len(ms):
                fail(f"index {idx} out of range ({len(ms)} match(es))")
            n = ms[idx]
            if "center" not in n:
                fail("matched element has no bounds to tap")
            note = f" ({len(ms)} matches, tapped #{idx}: {fmt_node(n)})" if len(ms) > 1 else f" ({fmt_node(n)})"
            r2, vnote = act(did, "input", {"action": "tap", "x": n["center"][0], "y": n["center"][1]})
            out(r2, "Tapped" + note + vnote + ".")
        else:
            if a.x is None or a.y is None:
                fail("tap needs coordinates (X Y) or a selector (--text / --desc / --id / --field N)")
            for v in (a.x, a.y):
                if not (0.0 <= v <= 1.0):
                    fail(f"coordinates are fractions between 0 and 1, not pixels (got {a.x}, {a.y}) — 0.5 0.5 is the middle of the screen; `tree` reports element centers as fractions")
            r, note = act(did, "input", {"action": "tap", "x": a.x, "y": a.y})
            out(r, f"Tapped{note}.")
    elif a.cmd == "swipe":
        for v in (a.x1, a.y1, a.x2, a.y2):
            if not (0.0 <= v <= 1.0):
                fail(f"coordinates are fractions between 0 and 1, not pixels (got {a.x1} {a.y1} -> {a.x2} {a.y2})")
        r, note = act(did, "input", {"action": "swipe", "x1": a.x1, "y1": a.y1,
                                      "x2": a.x2, "y2": a.y2, "dur": a.ms})
        out(r, f"Swiped{note}.")
    elif a.cmd == "key":
        r, note = act(did, "input", {"action": "key", "key": a.code})
        out(r, f"Key sent{note}.")
    elif a.cmd == "text":
        r, note = act(did, "input", {"action": "text", "text": a.text})
        out(r, f"Text typed{note}.")
    elif a.cmd == "shot":
        body, tries = fetch_screenshot(did, a.scale)
        with open(a.out, "wb") as f:
            f.write(body)
        resumed = f" (resumed — the transfer was cut {tries - 1}x and picked up where it left off)" if tries > 1 else ""
        out({"file": a.out, "bytes": len(body), "requests": tries},
            f"Screenshot saved to {a.out} ({len(body)} bytes){resumed}")
    elif a.cmd == "handoff":
        r, note = act(did, "handoff", {"driver": a.driver}, kind="session", want_driver=a.driver)
        out(r, f"The wheel now belongs to: {a.driver}{note}")
    elif a.cmd == "request-human":
        reason = " ".join(a.reason.split())[:200]
        if not reason:
            fail("request-human needs a reason — one short line saying what the human needs to do")
        r, note = act(did, "request-human", {"reason": reason}, kind="session")
        if r.get("already_user"):
            out(r, "The human already has the wheel on this device — no request needed.")
        else:
            out(r, "Help requested — the portal now shows the banner. "
                   "Send the human this message, word for word:\n---\n"
                   + (r.get("message_for_human") or "")
                   + f"\n---\nThen park on the handback: previewapk wait --for driver{note}")
    elif a.cmd == "finish":
        summary = " ".join(a.summary.split())[:1000]
        payload = {"summary": summary, "end_session": a.end_session}
        if a.verdict:
            payload["verdict"] = a.verdict
        r, note = act(did, "finish", payload, kind="session")
        out(r, "Wrap-up filed. Send the human this message, word for "
               "word:\n---\n" + (r.get("message_for_human") or "")
               + f"\n---{note}")
    elif a.cmd == "relaunch":
        r, note = act(did, "input", {"action": "launch"})
        out(r, f"App relaunched — brought back to the foreground (no reboot){note}.")
    elif a.cmd == "wait":
        if getattr(a, "wait_for", "element") == "driver":
            # Handback wait (2026-10-23): poll state until the human
            # hands the wheel back. The poll that first sees
            # driver=agent is the read the server delivers the
            # handback note on, so the note rides this result.
            timeout_s = a.timeout if a.timeout is not None else 300
            t0 = time.time()
            while True:
                st = get_state()
                save_selection(pick_device(st, a.device))
                dv = next((x for x in st.get("devices", [])
                           if x["id"] == did), None)
                waited = int((time.time() - t0) * 1000)
                if dv is None:
                    out({"found": False, "waited_ms": waited,
                         "reason": "device_gone"},
                        "The device is gone from the account.")
                    sys.exit(3)
                if dv.get("status") != "live":
                    out({"found": False, "waited_ms": waited,
                         "reason": "session_ended",
                         "status": dv.get("status")},
                        f"The session ended while waiting ({dv.get('status')}).")
                    sys.exit(3)
                if dv.get("driver") == "agent":
                    r2 = {"found": True, "waited_ms": waited,
                          "driver": "agent",
                          "driver_note": dv.get("driver_note"),
                          "handoff": dv.get("handoff")}
                    human = (f"The wheel is yours again after {waited}ms.")
                    if dv.get("resumed"):
                        r2["resumed"] = True
                        r2["handback_note"] = dv.get("handback_note") or ""
                        r2["handback_at"] = dv.get("handback_at")
                        human += (f"\nHuman's note: "
                                  f"{dv.get('handback_note') or '(none)'}")
                    out(r2, human)
                    return
                if time.time() - t0 >= timeout_s:
                    out({"found": False, "waited_ms": waited,
                         "driver": dv.get("driver"),
                         "handoff": dv.get("handoff")},
                        f"Timed out after {waited}ms — the human still "
                        f"has the wheel.")
                    sys.exit(3)
                time.sleep(3)
        if not (a.text or a.desc or a.rid):
            fail("wait needs a selector: --text, --desc, or --id (or --for driver)")
        timeout_s = a.timeout if a.timeout is not None else 30
        payload = {"action": "wait_for", "present": not a.gone,
                   "timeout_s": timeout_s}
        if a.text:
            payload["text"] = a.text
        if a.desc:
            payload["desc"] = a.desc
        if a.rid:
            payload["id"] = a.rid
        r = dev_action(did, "input", payload,
                       timeout=min(240, int(timeout_s) + 60))
        sel = a.text or a.desc or a.rid
        if r.get("found"):
            out(r, f"'{sel}' {'appeared' if not a.gone else 'disappeared'} after {r.get('waited_ms', 0)}ms.")
        else:
            out(r, f"Timed out after {r.get('waited_ms', 0)}ms — '{sel}' still {'absent' if not a.gone else 'present'}.")
            sys.exit(3)
    elif a.cmd == "act":
        # A batch is NEVER auto-replayed: a dropped connection means the
        # outcome is honestly unknown (exit 2) — earlier actions may have
        # landed. This deliberately bypasses the receipt helper.
        raw = sys.stdin.read() if a.file == "-" else open(a.file).read()
        try:
            spec = json.loads(raw)
        except Exception as e:
            fail(f"batch file is not valid JSON: {e}")
        if isinstance(spec, list):
            spec = {"actions": spec}
        actions = spec.get("actions")
        if not isinstance(actions, list) or not actions:
            fail("batch JSON needs a non-empty \"actions\" list")
        payload = {"action": "act", "actions": actions,
                   "observe": spec.get("observe", "none")}
        try:
            r = dev_action(did, "input", payload, timeout=330)
        except TransportError:
            out({"landed": None, "action": "act"},
                "The connection dropped mid-batch and a batch is never "
                "replayed blind. Some actions may have landed — check "
                "with `previewapk look` before resubmitting, and resubmit "
                "only from the first action you cannot confirm.")
            sys.exit(2)
        lines = []
        for res1 in r.get("results", []):
            line = f"#{res1.get('index')} {res1.get('type')}: {'ok' if res1.get('ok') else 'FAILED'} ({res1.get('ms', 0)}ms)"
            er = res1.get("error") or {}
            if er:
                line += f" — {er.get('message', '')} [{er.get('code', '')}]"
                if er.get("next"):
                    line += f"\n    next: {er['next']}"
                for ci, c in enumerate(er.get("candidates") or []):
                    line += f"\n    candidate {ci}: {c.get('type', '?')} {c.get('text') or c.get('desc') or ''!r} @{c.get('center')}"
            lines.append(line)
        lines.append(f"completed {r.get('completed', 0)}/{len(actions)}")
        shot_note = ""
        if r.get("screenshot_b64"):
            import base64 as _b64
            data = _b64.b64decode(r["screenshot_b64"])
            with open(a.shot_out, "wb") as f:
                f.write(data)
            shot_note = f"Post-batch screenshot saved to {a.shot_out} ({len(data)} bytes)."
            if a.json:
                r = {**r, "screenshot_b64": f"<{len(data)} bytes, saved to {a.shot_out}>"}
        if (r.get("observe") or {}).get("text"):
            lines.append(r["observe"]["text"])
        if shot_note:
            lines.append(shot_note)
        out(r, "\n".join(lines))
        if not r.get("ok"):
            sys.exit(1)
    elif a.cmd == "record":
        def rec_call(payload, timeout=90):
            try:
                return dev_action(did, "input", payload, timeout=timeout)
            except TransportError:
                fail("the connection dropped mid-call — check `previewapk record status` before repeating anything; a recording may already be running or already pulled")
        if a.record_cmd == "start":
            r = rec_call({"action": "record_start", "name": a.name or ""})
            out(r, f"Recording started ({r.get('rec_id')}). Stop it with `previewapk record stop` — a single recording caps at 180s.")
        elif a.record_cmd == "stop":
            r = rec_call({"action": "record_stop"}, timeout=330)
            dur = f", {r.get('duration_s')}s" if r.get("duration_s") else ""
            out(r, f"Recording stopped: {r.get('rec_id')} ({r.get('size', 0)} bytes{dur}). Download it with `previewapk record pull`.")
        elif a.record_cmd == "status":
            r = rec_call({"action": "record_status"}, timeout=330)
            if r.get("recording"):
                human = f"Recording: {r.get('rec_id')} — {r.get('elapsed_s')}s in."
            elif r.get("last"):
                l = r["last"]
                human = f"Not recording. Last: {l.get('rec_id')} ({l.get('size', 0)} bytes, {l.get('duration_s')}s) — `previewapk record pull` downloads it."
            else:
                human = "Not recording, and no finished recordings for this session yet."
            out(r, human)
        elif a.record_cmd == "pull":
            rec_id = a.rec_id
            if not rec_id:
                r = rec_call({"action": "record_status"}, timeout=330)
                last = r.get("last")
                if r.get("recording"):
                    fail("a recording is still running — stop it first (`previewapk record stop`), then pull")
                if not last:
                    fail("no finished recording for this session yet — `previewapk record start`, drive, `previewapk record stop`, then pull")
                rec_id = last["rec_id"]
            data = fetch_binary(f"/portal/api/device/{did}/recording/{rec_id}")
            with open(a.out, "wb") as f:
                f.write(data)
            out({"file": a.out, "rec_id": rec_id, "bytes": len(data)},
                f"Recording saved to {a.out} ({len(data)} bytes).")
    elif a.cmd == "device":
        def dev_call(payload, timeout=150):
            try:
                return dev_action(did, "input", payload, timeout=timeout)
            except TransportError:
                fail("the connection dropped mid-call — the change may or may not have landed; verify (status, look, or the matching device command) before repeating it")
        base = {"action": "device"}
        if a.device_cmd == "timezone":
            r = dev_call({**base, "op": "timezone", "tz": a.tz})
            out(r, f"Device timezone is now {r.get('timezone')}.")
        elif a.device_cmd == "locale":
            pl = {**base, "op": "locale", "locale": a.locale}
            if a.package:
                pl["package"] = a.package
            r = dev_call(pl)
            out(r, f"{r.get('package')} now runs in {r.get('locale')} (per-app language). Relaunch the app if it doesn't switch on its own.")
        elif a.device_cmd == "location":
            r = dev_call({**base, "op": "location", "lat": a.lat, "lon": a.lon})
            out(r, f"GPS now reports {r.get('lat')}, {r.get('lon')} (mock fix). Clear it with `previewapk device location-clear`.")
        elif a.device_cmd == "location-clear":
            r = dev_call({**base, "op": "location_clear"})
            out(r, "Mock GPS fix removed.")
        elif a.device_cmd == "push":
            import base64 as _b64d
            with open(a.local, "rb") as f:
                raw = f.read()
            if len(raw) > 10 * 1024 * 1024:
                fail(f"{a.local} is {len(raw)} bytes — device file pushes cap at 10 MB")
            remote = a.remote or ("Download/" + os.path.basename(a.local))
            r = dev_call({**base, "op": "file_push", "path": remote,
                          "data_b64": _b64d.b64encode(raw).decode()}, timeout=240)
            out(r, f"Pushed {r.get('bytes')} bytes to {r.get('path')} on the device.")
        elif a.device_cmd == "pull":
            from urllib.parse import quote as _q
            data = fetch_binary(f"/portal/api/device/{did}/file?path=" + _q(a.remote))
            outp = a.out or os.path.basename(a.remote.rstrip("/"))
            with open(outp, "wb") as f:
                f.write(data)
            out({"file": outp, "remote": a.remote, "bytes": len(data)},
                f"Pulled {a.remote} -> {outp} ({len(data)} bytes).")
        elif a.device_cmd == "ls":
            r = dev_call({**base, "op": "file_list", "path": a.path})
            rows = [f"{'d' if e.get('dir') else '-'} {e.get('size', 0):>10}  {e.get('name')}"
                    for e in r.get("entries", [])]
            out(r, f"{r.get('path')}:\n" + ("\n".join(rows) if rows else "(empty)"))
        elif a.device_cmd == "rm":
            r = dev_call({**base, "op": "file_delete", "path": a.path})
            out(r, f"Deleted {r.get('path')} from the device.")
        elif a.device_cmd == "reboot":
            r = dev_call({**base, "op": "reboot"}, timeout=300)
            extra = f" A recording was stopped and saved first ({r.get('salvaged_recording')})." if r.get("salvaged_recording") else ""
            out(r, f"Phone restarted — same phone, same data. The app {'came back to the foreground' if r.get('relaunched') else 'did not relaunch automatically; use `previewapk relaunch`'}.{extra}")


if __name__ == "__main__":
    main()
