#!/usr/bin/env python3
"""PreviewAPK MCP server — give your AI agent eyes and hands on a real
Android device.

The agent can push an APK build to the owner's PreviewAPK device, start a
preview session, drive the app (tap / swipe / keys / type), take
screenshots it can actually see, hand control to the human, and stop.

Setup:
  1. Download this file:  curl -o previewapk_mcp.py https://previewapk.siteport.site/mcp/previewapk_mcp.py
  2. Get the API key from the portal (Developers card).
  3. Register the server with your agent, e.g. Claude Code:
       claude mcp add previewapk --env PREVIEWAPK_API_KEY=<key> -- python3 /path/to/previewapk_mcp.py
     or in a generic MCP config:
       {"mcpServers": {"previewapk": {"command": "python3",
          "args": ["/path/to/previewapk_mcp.py"],
          "env": {"PREVIEWAPK_API_KEY": "<key>"}}}}

Optional env: PREVIEWAPK_HOST (default https://previewapk.siteport.site),
PREVIEWAPK_DEVICE (default: the account's first device).

Driver modes: start with driver="agent" to drive it yourself (the human
watches); driver="user" to present it to the human, who drives in the
portal while your input is refused. previewapk_handoff switches sides
mid-session without rebooting.
"""
import base64
import hashlib
import http.client
import json
import os
import sys
import urllib.error
import time
import urllib.request
import uuid

VERSION = "1.0.5"
HOST = os.environ.get("PREVIEWAPK_HOST", "https://previewapk.siteport.site").rstrip("/")
KEY = os.environ.get("PREVIEWAPK_API_KEY", "")
DEFAULT_DEVICE = os.environ.get("PREVIEWAPK_DEVICE", "")


class TransportError(Exception):
    """The request or its response died in transit; the server may or
    may not have acted. Verify state — never assume either way."""


def _api_once(method, path, payload, timeout, raw):
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
        body = e.read()
        try:
            err = json.loads(body).get("error", f"HTTP {e.code}")
        except Exception:
            err = f"HTTP {e.code}"
        raise RuntimeError(err)
    except Exception as e:
        raise TransportError(str(e))
    return body if raw else json.loads(body or b"{}")


def api(method, path, payload=None, timeout=60, raw=False, raise_transport=False):
    if not KEY:
        raise RuntimeError("PREVIEWAPK_API_KEY is not set")
    attempts = 4 if method == "GET" else 1
    for i in range(attempts):
        try:
            return _api_once(method, path, payload, timeout, raw)
        except TransportError as e:
            if i + 1 < attempts:
                time.sleep(min(0.7 * (2 ** i), 2.5))
                continue
            if raise_transport:
                raise
            raise RuntimeError(f"the connection to PreviewAPK dropped mid-request ({e}) — "
                               "if this was an action, check the device state before retrying it")


SELECTED = {"id": ""}  # this agent session's working device


def pick_device(want):
    try:
        state = api("GET", "/portal/api/state", raise_transport=True)
    except TransportError:
        raise RuntimeError("the connection to PreviewAPK dropped while checking device state — "
                           "the network cut it repeatedly and nothing was attempted. "
                           "Wait a moment and retry.")
    devs = state.get("devices", [])
    if not devs:
        raise RuntimeError("no devices on this account")
    want = want or SELECTED["id"] or DEFAULT_DEVICE
    if want:
        for d in devs:
            if d["id"] == want:
                return d, state
        for d in devs:
            if (d.get("name") or "").lower() == want.lower():
                return d, state
        raise RuntimeError(f"no device '{want}' (have: {', '.join(d.get('name', d['id']) for d in devs)})")
    raise RuntimeError("no working device — call previewapk_list_devices, then select one with previewapk_use_device (or start one with previewapk_start, which selects it). Nothing is guessed: an agent must state where it is working.")


def act(device, action, payload=None):
    return api("POST", f"/portal/api/device/{device}/{action}", payload or {})


def screen_digest(device):
    try:
        r = api("GET", f"/portal/api/device/{device}/hierarchy",
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


def act_input(device, payload):
    """An input action with a receipt. Returns the server result, or a
    dict saying the connection died and what the state check found —
    {"landed": true} when the screen changed, {"landed": null} when it
    could not be settled. Never a bare connection error."""
    before = screen_digest(device)
    try:
        return api("POST", f"/portal/api/device/{device}/input", payload, raise_transport=True)
    except TransportError:
        pass
    time.sleep(0.9)
    after = screen_digest(device)
    if before is not None and after is not None:
        if after != before:
            return {"ok": True, "landed": True,
                    "note": "the connection dropped mid-call, but the screen changed — the input landed"}
        try:
            return api("POST", f"/portal/api/device/{device}/input", payload, raise_transport=True)
        except TransportError:
            pass
    return {"ok": False, "landed": None,
            "note": "the connection dropped mid-call and the state check could not settle whether the input landed — check previewapk_hierarchy before repeating it"}


def fetch_screenshot(device, scale=1.0):
    """Resumable screenshot download (Range + the portal's frame
    cache): a cut transfer picks up at the byte it reached."""
    path = f"/portal/api/device/{device}/screenshot"
    if scale and scale != 1.0:
        path += f"?scale={scale}"
    buf = bytearray()
    total = None
    cap = None
    for _ in range(24):
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
            if e.code in (503, 416):
                buf, total, cap = bytearray(), None, None
                time.sleep(1.5)
                continue
            raise RuntimeError(f"HTTP {e.code}: screenshot failed")
        except Exception:
            time.sleep(0.5)
            continue
        if status == 200 and buf:
            buf, total, cap = bytearray(), None, None
        if status in (200, 206):
            this_cap = hdrs.get("X-Captured-At")
            if cap is not None and this_cap and this_cap != cap:
                buf, total, cap = bytearray(), None, None
                continue
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
                        continue
                buf.extend(chunk)
        if total is not None and len(buf) >= total:
            break
        time.sleep(0.3)
    data = bytes(buf)
    if not data.startswith(b"\x89PNG") or b"IEND" not in data[-12:]:
        raise RuntimeError("screenshot transfer could not be completed — the network kept cutting it")
    return data


def upload_apk(path, device):
    boundary = "----pvk" + uuid.uuid4().hex
    with open(path, "rb") as f:
        raw = f.read()
    name = os.path.basename(path)
    parts = []
    if device:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="device"\r\n\r\n{device}\r\n'.encode())
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="apk"; filename="{name}"\r\n'
                  f'Content-Type: application/vnd.android.package-archive\r\n\r\n').encode())
    parts.append(raw)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    r = urllib.request.Request(HOST + "/portal/api/upload", data=b"".join(parts), method="POST",
                               headers={"X-Api-Key": KEY,
                                        "Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(r, timeout=300) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            err = json.loads(body).get("error", "upload failed")
        except Exception:
            err = "upload failed"
        raise RuntimeError(err)
    except Exception as e:
        raise TransportError(str(e))


def _ver(s):
    try:
        return tuple(int(x) for x in str(s).split("."))
    except Exception:
        return ()


def upgrade_notice(state):
    """One plain line when the server knows a newer MCP client than
    this one. Silent when the server doesn't say."""
    info = ((state or {}).get("clients") or {}).get("mcp") or {}
    latest = info.get("version")
    if latest and _ver(latest) > _ver(VERSION):
        return (f"previewapk mcp {latest} is available (you're running {VERSION}): "
                f"curl -o {info.get('file', 'previewapk_mcp.py')} {info.get('url', '')}")
    return ""


def text_result(obj):
    return {"content": [{"type": "text", "text": json.dumps(obj, indent=1)}]}


TOOLS = [
    {"name": "previewapk_list_devices",
     "description": "List the devices on this account: name, id, profile, Android version, status, attached app. Call this FIRST when the account may have more than one device, then pass the chosen device's name (or id) as the 'device' argument to the other tools.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "previewapk_use_device",
     "description": "Select your working device — the 'cd' of PreviewAPK. After selecting, every other tool talks to this device until you select another or start one (starting also selects). Pass the device NAME (e.g. 'Device 1') or its id, from previewapk_list_devices.",
     "inputSchema": {"type": "object", "required": ["device"], "properties": {
         "device": {"type": "string", "description": "device name or id"}}}},
    {"name": "previewapk_status",
     "description": "State of the owner's PreviewAPK device(s): attached app, session status, driver, links.",
     "inputSchema": {"type": "object", "properties": {"device": {"type": "string", "description": "device name or id (optional)"}}}},
    {"name": "previewapk_push_apk",
     "description": "Upload an APK file from this machine to the device. If the device is OFF it becomes the attached app (then call previewapk_start). If the device is RUNNING and you hold the wheel, the new app is hot-swapped in live — installed and relaunched with no reboot. Returns the parsed app info (swapped=true on a hot swap).",
     "inputSchema": {"type": "object", "required": ["path"], "properties": {
         "path": {"type": "string", "description": "local path to the .apk file"},
         "device": {"type": "string"}}}},
    {"name": "previewapk_start",
     "description": "Start a preview session for the device's attached app. driver='agent': you drive and screenshot; the human watches. driver='user': the human drives in the portal; your input is refused. Returns links info — share the session_url from previewapk_get_links with the human.",
     "inputSchema": {"type": "object", "required": ["device"], "properties": {
         "driver": {"type": "string", "enum": ["agent", "user"], "default": "agent"},
         "device": {"type": "string"}}}},
    {"name": "previewapk_stop",
     "description": "Stop the session. The session link dies with it. (previewapk_feedback files a field report about the tool — what broke, what worked, what you wish it had; reports directly shape what gets fixed and built next.)",
     "inputSchema": {"type": "object", "properties": {"device": {"type": "string"}}}},
    {"name": "previewapk_get_links",
     "description": "The share link for the live session (view-only; minted at start, dies when the session ends), plus driver state — including driver_note, which tells you if the user took the wheel.",
     "inputSchema": {"type": "object", "properties": {"device": {"type": "string"}}}},
    {"name": "previewapk_tap",
     "description": "Tap the screen. Coordinates are 0..1 fractions of the screen (resolution-independent). Requires an agent-driven session.",
     "inputSchema": {"type": "object", "required": ["x", "y"], "properties": {
         "x": {"type": "number"}, "y": {"type": "number"}, "device": {"type": "string"}}}},
    {"name": "previewapk_swipe",
     "description": "Swipe from (x1,y1) to (x2,y2), 0..1 fractions. duration_ms optional (default 300).",
     "inputSchema": {"type": "object", "required": ["x1", "y1", "x2", "y2"], "properties": {
         "x1": {"type": "number"}, "y1": {"type": "number"},
         "x2": {"type": "number"}, "y2": {"type": "number"},
         "duration_ms": {"type": "integer"}, "device": {"type": "string"}}}},
    {"name": "previewapk_press_key",
     "description": "Send an Android keycode: 4=Back, 3=Home, 187=Recents, 66=Enter, 67=Delete.",
     "inputSchema": {"type": "object", "required": ["keycode"], "properties": {
         "keycode": {"type": "integer"}, "device": {"type": "string"}}}},
    {"name": "previewapk_type_text",
     "description": "Type text into the currently focused field on the device. The field must be focused first (tap it) — the call fails honestly when nothing is focused, instead of typing into the void.",
     "inputSchema": {"type": "object", "required": ["text"], "properties": {
         "text": {"type": "string"}, "device": {"type": "string"}}}},
    {"name": "previewapk_relaunch",
     "description": "Bring the session's app back to the foreground — after Back exits to the launcher, or the app crashed or was closed. Takes about 2 seconds; no reboot.",
     "inputSchema": {"type": "object", "properties": {"device": {"type": "string"}}}},
    {"name": "previewapk_screenshot",
     "description": "Capture the device's current screen as an image you can see (the real framebuffer — it agrees with the hierarchy). Use it to verify your changes look right. The download resumes automatically if the connection is cut; pass scale=0.5 on weak networks for a quarter-size transfer.",
     "inputSchema": {"type": "object", "properties": {"device": {"type": "string"}, "scale": {"type": "number", "description": "0.5 = half-size frame (smaller transfer)"}}}},
    {"name": "previewapk_handoff",
     "description": "Pass the wheel mid-session: to='user' lets the human drive in the portal (e.g. after you've verified a feature); to='agent' gives control back to you.",
     "inputSchema": {"type": "object", "required": ["to"], "properties": {
         "to": {"type": "string", "enum": ["agent", "user"]}, "device": {"type": "string"}}}},
    {"name": "previewapk_get_logs",
     "description": "Read the attached app's debug log (logcat) from this session — its prints, warnings, errors and crash traces. Check it after actions and whenever something misbehaves: a crash's exact reason is here. Works after the session ends too (the saved tail of the last session).",
     "inputSchema": {"type": "object", "properties": {
         "since_seconds": {"type": "number", "description": "only lines from the last N seconds"},
         "lines": {"type": "integer", "default": 100},
         "errors_only": {"type": "boolean", "default": False},
         "contains": {"type": "string", "description": "only lines containing this text"},
         "device": {"type": "string"}}}},
    {"name": "previewapk_feedback",
     "description": "File a field report about THIS tool — you are one of the few who actually use it, and your report is read and tallied by the team that builds it. Say what broke or behaved unexpectedly (broke), what worked well (worked), what feature you wished it had for your task (wish), and anything you had to work around (workaround). One call, any subset of fields. File it before you stop the session, while the details are fresh — the wishes of working agents are the roadmap.",
     "inputSchema": {"type": "object", "properties": {
         "worked": {"type": "string"}, "broke": {"type": "string"},
         "wish": {"type": "string"}, "workaround": {"type": "string"},
         "rating": {"type": "integer", "minimum": 1, "maximum": 5,
                    "description": "overall reliability this session, 1-5"},
         "device": {"type": "string"}}}},
    {"name": "previewapk_hierarchy",
     "description": "Get the current screen as a UI element tree (class, text, content-desc, resource id, bounds, center coordinates, state flags) instead of pixels. Much faster and cheaper than a screenshot for navigation and assertions: find elements, read values, check state. Use previewapk_screenshot for visual checks (layout, overlap, images).",
     "inputSchema": {"type": "object", "properties": {"device": {"type": "string"}}}},
    {"name": "previewapk_tap_text",
     "description": "Tap the element whose visible text matches (exact match preferred, else substring; use index to pick among matches). Resolves the element's center from the UI hierarchy — prefer this over coordinate taps whenever the target has text.",
     "inputSchema": {"type": "object", "required": ["text"], "properties": {
         "text": {"type": "string"}, "index": {"type": "integer", "default": 0}, "device": {"type": "string"}}}},
]


def call_tool(name, args):
    args = args or {}
    want = args.get("device", "")
    if name == "previewapk_push_apk":
        d0, _s0 = pick_device(want)
        try:
            res = upload_apk(args["path"], d0["id"])
        except TransportError:
            try:
                st = api("GET", "/portal/api/state", raise_transport=True)
                dv = next((x for x in st.get("devices", []) if x["id"] == d0["id"]), {})
                app = (dv.get("apk") or {}).get("label")
                return text_result({"ok": False, "landed": None,
                                    "attached_app": app, "device_status": dv.get("status"),
                                    "note": "the connection dropped during the upload and it could not be settled whether the APK arrived — the state check above shows what is attached now; re-push if it is not your build"})
            except TransportError:
                return text_result({"ok": False, "landed": None,
                                    "note": "the connection dropped during the upload and the follow-up state check was cut too — check previewapk_status before re-pushing"})
        return text_result(res)
    if name == "previewapk_list_devices":
        state = api("GET", "/portal/api/state")
        return text_result({"devices": [
            {"id": dv["id"], "name": dv.get("name"), "profile": dv.get("profile"),
             "android": dv.get("android"), "status": dv.get("status"),
             "selected": bool(SELECTED["id"]) and dv["id"] == SELECTED["id"],
             "app": (dv.get("apk") or {}).get("label")} for dv in state.get("devices", [])]})
    if name == "previewapk_use_device":
        d, _s = pick_device(args.get("device", ""))
        SELECTED["id"] = d["id"]
        return text_result({"selected": d.get("name"), "id": d["id"], "status": d.get("status"),
                            "note": "Working device set. Other tools now talk to this device until you select or start another."})
    d, _state = pick_device(want)
    did = d["id"]
    if name == "previewapk_status":
        un = upgrade_notice(_state)
        return text_result({**d, "client_notice": un} if un else d)
    if name == "previewapk_start":
        SELECTED["id"] = did
        r = act(did, "run", {"driver": args.get("driver", "agent")})
        links = act(did, "links")
        un = upgrade_notice(_state)
        return text_result({**r, "links": links, **({"client_notice": un} if un else {}),
                            "note": "The session takes seconds to boot. Poll previewapk_status until status is 'live', then screenshot. Share the session_url from previewapk_get_links with the human."
                            + (f" Also: {un}" if un else "")})
    if name == "previewapk_feedback":
        if _state.get("share_diagnostics") is False:
            raise RuntimeError("diagnostic sharing is turned off for this account — the owner opted out in the portal's Privacy settings, so no report was sent")
        rep = {k: args[k] for k in ("worked", "broke", "wish", "workaround", "rating")
               if args.get(k)}
        if not rep:
            raise RuntimeError("a field report needs at least one of: worked, broke, wish, workaround")
        rep["client"] = "mcp"
        rep["client_version"] = VERSION
        rep["device"] = did
        return text_result(api("POST", "/portal/api/feedback", rep))
    if name == "previewapk_stop":
        r = act(did, "stop")
        if _state.get("share_diagnostics") is False:
            return text_result(r)
        return text_result({**r, "note": "Session stopped. You drove it — file a field report while it's fresh with previewapk_feedback: what broke, what worked, what you wish it had. Reports are read and tallied; they decide what gets fixed and built next."})
    if name == "previewapk_get_links":
        return text_result(act(did, "links"))
    if name == "previewapk_tap":
        return text_result(act_input(did, {"action": "tap", "x": args["x"], "y": args["y"]}))
    if name == "previewapk_swipe":
        return text_result(act_input(did, {"action": "swipe", "x1": args["x1"], "y1": args["y1"],
                                              "x2": args["x2"], "y2": args["y2"],
                                              "dur": args.get("duration_ms", 300)}))
    if name == "previewapk_press_key":
        return text_result(act_input(did, {"action": "key", "key": args["keycode"]}))
    if name == "previewapk_type_text":
        return text_result(act_input(did, {"action": "text", "text": args["text"]}))
    if name == "previewapk_relaunch":
        return text_result(act_input(did, {"action": "launch"}))
    if name == "previewapk_screenshot":
        raw = fetch_screenshot(did, args.get("scale", 1.0))
        return {"content": [{"type": "image", "data": base64.b64encode(raw).decode(),
                             "mimeType": "image/png"}]}
    if name == "previewapk_get_logs":
        import urllib.parse as _up
        params = {"lines": int(args.get("lines", 100))}
        if args.get("since_seconds"):
            params["since"] = float(args["since_seconds"])
        if args.get("errors_only"):
            params["level"] = "E"
        if args.get("contains"):
            params["q"] = args["contains"]
        r = api("GET", f"/portal/api/device/{did}/logs?" + _up.urlencode(params), timeout=60)
        shown = [e for e in r.get("lines", [])
                 if not (args.get("errors_only") and e.get("env"))]
        body = "\n".join(e["raw"] for e in shown) or "(no log lines matched)"
        head = f"{len(shown) if args.get('errors_only') else r.get('count', 0)} line(s) from {d.get('name')}"
        if r.get("ended"):
            head += " — session ended, saved tail of the last session"
        return {"content": [{"type": "text", "text": head + "\n" + body}]}
    if name == "previewapk_hierarchy":
        r = api("GET", f"/portal/api/device/{did}/hierarchy", timeout=90)
        lines = []

        def fmt(n):
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

        def walk(n, depth=0):
            if not n:
                return
            lines.append("  " * depth + fmt(n))
            for c in n.get("children", []):
                walk(c, depth + 1)
        walk(r.get("tree"))
        head = f"package: {r.get('package')}  screen: {r.get('width')}x{r.get('height')}"
        if r.get("truncated"):
            head += "  (tree truncated at 500 elements)"
        return {"content": [{"type": "text", "text": head + "\n" + "\n".join(lines)}]}
    if name == "previewapk_tap_text":
        r = api("GET", f"/portal/api/device/{did}/hierarchy", timeout=90)
        flat = []

        def collect(n):
            if not n:
                return
            flat.append(n)
            for c in n.get("children", []):
                collect(c)
        collect(r.get("tree"))
        want_text = args["text"]
        # Labels live in `text` on native views, in `desc` on
        # Flutter/semantics-driven UIs — search both fields.
        ms = []
        for field in ("text", "desc"):
            ms = [n for n in flat if n.get(field) == want_text]
            if ms:
                break
        if not ms:
            ms = [n for n in flat if want_text.lower() in (n.get("text") or "").lower()
                  or want_text.lower() in (n.get("desc") or "").lower()]
        if not ms:
            raise RuntimeError(f"no element with text matching '{want_text}' on the current screen")
        idx = int(args.get("index", 0))
        if idx >= len(ms):
            raise RuntimeError(f"index {idx} out of range ({len(ms)} match(es))")
        n = ms[idx]
        if "center" not in n:
            raise RuntimeError("matched element has no bounds to tap")
        res = act_input(did, {"action": "tap", "x": n["center"][0], "y": n["center"][1]})
        return text_result({**res, "tapped": {"text": n.get("text"), "class": n.get("class"),
                                             "center": n["center"], "matches": len(ms)}})
    if name == "previewapk_handoff":
        return text_result(act(did, "handoff", {"driver": args["to"]}))
    raise RuntimeError(f"unknown tool {name}")


def handle(msg):
    method = msg.get("method", "")
    mid = msg.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "previewapk", "version": VERSION}}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params", {})
        try:
            result = call_tool(params.get("name", ""), params.get("arguments", {}))
            return {"jsonrpc": "2.0", "id": mid, "result": result}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": f"error: {e}"}], "isError": True}}
    if mid is not None:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": f"method not found: {method}"}}
    return None


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        resp = handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
