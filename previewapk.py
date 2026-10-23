#!/usr/bin/env python3
"""previewapk — push an APK to your PreviewAPK device, get a preview link.

Made for developers, CI jobs, and AI agents. No dependencies (stdlib only).

Setup:
  export PREVIEWAPK_API_KEY=<your key from the portal's Developers card>
  # optional: PREVIEWAPK_HOST (default https://previewapk.siteport.site)
  # PREVIEWAPK_DEVICE pins a device; without it every command needs --device

Commands:
  previewapk push app.apk [--run] [--driver agent|user] [--device dev1]
  previewapk start [--driver agent|user]     start the device's attached app
  previewapk stop                             stop the device (the session link dies)
  previewapk status                           device + session state
  previewapk links                            the share link for the live session
  previewapk tap X Y                          X,Y are 0..1 screen fractions
  previewapk swipe X1 Y1 X2 Y2 [MS]
  previewapk key CODE                         Android keycode (4=Back, 3=Home)
  previewapk text "hello"                     type into the focused field
  previewapk tree                             the UI element tree (text + tap coordinates) — the fast, reliable way to verify what is on screen
  previewapk shot [out.png] [--scale 0.5]     save a screenshot (resumable download; --scale shrinks it for weak networks)
  previewapk relaunch                         bring the app back to the foreground (no reboot)
  previewapk handoff user|agent               pass the wheel mid-session
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

VERSION = "1.0.5"
HOST = os.environ.get("PREVIEWAPK_HOST", "https://previewapk.siteport.site").rstrip("/")
KEY = os.environ.get("PREVIEWAPK_API_KEY", "")


def fail(msg):
    print(f"previewapk: {msg}", file=sys.stderr)
    sys.exit(1)


class TransportError(Exception):
    """The request or its response died in transit. The server may or
    may not have acted — callers must verify state, never assume."""


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
        body = e.read()
        try:
            err = json.loads(body).get("error", body.decode("utf-8", "replace"))
        except Exception:
            err = body.decode("utf-8", "replace")
        fail(f"HTTP {e.code}: {err}")
    except Exception as e:
        raise TransportError(str(e))
    return body if raw else json.loads(body or b"{}")


def req(method, path, payload=None, timeout=60, raw=False, raise_transport=False):
    if not KEY:
        fail("PREVIEWAPK_API_KEY is not set (find it in your portal's Developers card)")
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


def upload_apk(path, device):
    if not KEY:
        fail("PREVIEWAPK_API_KEY is not set")
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
    body = b"".join(parts)
    r = urllib.request.Request(HOST + "/portal/api/upload", data=body, method="POST",
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
        fail(f"HTTP {e.code}: {err}")
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


def dev_action(device, action, payload=None):
    return req("POST", f"/portal/api/device/{device}/{action}", payload or {},
               raise_transport=True)


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
    p = sub.add_parser("push", parents=[common], help="push an APK to the device (attach it; --run starts a session)"); p.add_argument("apk"); p.add_argument("--run", action="store_true")
    p.add_argument("--driver", choices=["agent", "user"], default="agent")
    p = sub.add_parser("start", parents=[common], help="start the device's attached app"); p.add_argument("--driver", choices=["agent", "user"], default="agent")
    sub.add_parser("stop", parents=[common], help="stop the device (the session link dies with the session)"); sub.add_parser("status", parents=[common], help="device + session state"); sub.add_parser("links", parents=[common], help="the share link for the live session")
    sub.add_parser("relaunch", parents=[common], help="bring the app back to the foreground (no reboot)")
    sub.add_parser("devices", parents=[common], help="list the devices on this account")
    p = sub.add_parser("use", parents=[common], help="select the working device"); p.add_argument("name", nargs="?")
    p = sub.add_parser("tap", parents=[common], help="tap by X Y fractions (0..1), or --text/--desc/--id/--field to tap an element"); p.add_argument("x", type=float, nargs="?"); p.add_argument("y", type=float, nargs="?")
    p.add_argument("--text"); p.add_argument("--desc"); p.add_argument("--id", dest="rid"); p.add_argument("--index", type=int, default=0)
    p.add_argument("--field", type=int, help="tap the Nth input field on screen (1-based)")
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
    p = sub.add_parser("feedback", parents=[common], help="file a field report: what broke, what worked, what you wish the tool had")
    p.add_argument("--worked"); p.add_argument("--broke"); p.add_argument("--wish")
    p.add_argument("--workaround"); p.add_argument("--rating", type=int, choices=[1, 2, 3, 4, 5])
    a = ap.parse_args()
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
            res = upload_apk(a.apk, d0["id"])
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
        human = (f"{d['name']} ({did}) — {d['status']}"
                 + (f", driven by {d.get('driver')}" if d["status"] == "live" else "")
                 + (f", app: {d['apk']['label']} v{d['apk'].get('version_name')}" if d.get("apk") else ", no app attached")
                 + (f"\nSession link: {d.get('view_url')}" if d.get("view_url") else "")
                 + (f"\n{un}" if un else ""))
        out({**d, "client_notice": un} if un else d, human)
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
    elif a.cmd == "tap":
        if a.field is not None:
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
            if a.index >= len(ms):
                fail(f"index {a.index} out of range ({len(ms)} match(es))")
            n = ms[a.index]
            if "center" not in n:
                fail("matched element has no bounds to tap")
            note = f" ({len(ms)} matches, tapped #{a.index}: {fmt_node(n)})" if len(ms) > 1 else f" ({fmt_node(n)})"
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
    elif a.cmd == "relaunch":
        r, note = act(did, "input", {"action": "launch"})
        out(r, f"App relaunched — brought back to the foreground (no reboot){note}.")


if __name__ == "__main__":
    main()
