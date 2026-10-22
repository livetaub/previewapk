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
  previewapk shot [out.jpg]                   save a screenshot of the screen
  previewapk handoff user|agent               pass the wheel mid-session

Add --json to any command for machine-readable output.

Driver modes: a session is driven by exactly one side. `--driver agent`
means THIS caller (the API key) may drive; the owner watches in the portal.
`--driver user` means the owner drives in the portal and API input is
refused. `handoff` switches sides on a live session without rebooting.
"""
import argparse
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

HOST = os.environ.get("PREVIEWAPK_HOST", "https://previewapk.siteport.site").rstrip("/")
KEY = os.environ.get("PREVIEWAPK_API_KEY", "")


def fail(msg):
    print(f"previewapk: {msg}", file=sys.stderr)
    sys.exit(1)


def req(method, path, payload=None, timeout=60, raw=False):
    if not KEY:
        fail("PREVIEWAPK_API_KEY is not set (find it in your portal's Developers card)")
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
        fail(f"request failed: {e}")
    return body if raw else json.loads(body or b"{}")


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


def get_state():
    return req("GET", "/portal/api/state")


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
    return req("POST", f"/portal/api/device/{device}/{action}", payload or {})


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
    p = sub.add_parser("push", parents=[common]); p.add_argument("apk"); p.add_argument("--run", action="store_true")
    p.add_argument("--driver", choices=["agent", "user"], default="agent")
    p = sub.add_parser("start", parents=[common]); p.add_argument("--driver", choices=["agent", "user"], default="agent")
    sub.add_parser("stop", parents=[common]); sub.add_parser("status", parents=[common]); sub.add_parser("links", parents=[common])
    sub.add_parser("relaunch", parents=[common])
    sub.add_parser("devices", parents=[common])
    p = sub.add_parser("use", parents=[common]); p.add_argument("name", nargs="?")
    p = sub.add_parser("tap", parents=[common]); p.add_argument("x", type=float, nargs="?"); p.add_argument("y", type=float, nargs="?")
    p.add_argument("--text"); p.add_argument("--desc"); p.add_argument("--id", dest="rid"); p.add_argument("--index", type=int, default=0)
    p.add_argument("--field", type=int, help="tap the Nth input field on screen (1-based)")
    sub.add_parser("tree", parents=[common])
    p = sub.add_parser("logs", parents=[common]); p.add_argument("--since", type=float); p.add_argument("--lines", type=int, default=200)
    p.add_argument("--level", choices=["V", "D", "I", "W", "E", "F"]); p.add_argument("--errors", action="store_true")
    p.add_argument("--grep", dest="q"); p.add_argument("--follow", action="store_true")
    p = sub.add_parser("swipe", parents=[common]); p.add_argument("x1", type=float); p.add_argument("y1", type=float)
    p.add_argument("x2", type=float); p.add_argument("y2", type=float); p.add_argument("ms", type=int, nargs="?", default=300)
    p = sub.add_parser("key", parents=[common]); p.add_argument("code", type=int)
    p = sub.add_parser("text", parents=[common]); p.add_argument("text")
    p = sub.add_parser("shot", parents=[common]); p.add_argument("out", nargs="?", default="previewapk-shot.png")
    p = sub.add_parser("handoff", parents=[common]); p.add_argument("driver", choices=["agent", "user"])
    a = ap.parse_args()
    a.json = getattr(a, "json", False)
    a.device = getattr(a, "device", None) or load_selection() or os.environ.get("PREVIEWAPK_DEVICE", "")

    def out(obj, human):
        print(json.dumps(obj, indent=1) if a.json else human)

    if a.cmd == "push":
        d0 = pick_device(get_state(), a.device)
        save_selection(d0)
        res = upload_apk(a.apk, d0["id"])
        dev_id = res.get("device_id") or d0["id"]
        human = (f"Pushed {res.get('app', {}).get('label', a.apk)} "
                 f"v{res.get('app', {}).get('version_name', '?')} to device {dev_id}.")
        if res.get("swapped"):
            human += (" The device was running — the new app was swapped in "
                      "live and relaunched, no reboot.")
        if a.run and not res.get("swapped"):
            r = dev_action(dev_id, "run", {"driver": a.driver})
            links = dev_action(dev_id, "links")
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
    d = pick_device(state, a.device)
    did = d["id"]

    if a.cmd == "start":
        save_selection(d)
        r = dev_action(did, "run", {"driver": a.driver})
        links = dev_action(did, "links")
        out({**r, "links": links},
            f"Session starting on {d['name']} (driver: {a.driver}).\nShare link: run `previewapk links` once it's live.")
    elif a.cmd == "stop":
        r = dev_action(did, "stop")
        out(r, f"{d['name']} stopped. The session link died with the session.")
    elif a.cmd == "status":
        human = (f"{d['name']} ({did}) — {d['status']}"
                 + (f", driven by {d.get('driver')}" if d["status"] == "live" else "")
                 + (f", app: {d['apk']['label']} v{d['apk'].get('version_name')}" if d.get("apk") else ", no app attached")
                 + (f"\nSession link: {d.get('view_url')}" if d.get("view_url") else ""))
        out(d, human)
    elif a.cmd == "links":
        r = dev_action(did, "links")
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
            r2 = dev_action(did, "input", {"action": "tap", "x": n["center"][0], "y": n["center"][1]})
            out(r2, f"Tapped input field #{a.field} ({fmt_node(n)}).")
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
            r2 = dev_action(did, "input", {"action": "tap", "x": n["center"][0], "y": n["center"][1]})
            out(r2, "Tapped" + note + ".")
        else:
            if a.x is None or a.y is None:
                fail("tap needs coordinates (X Y) or a selector (--text / --desc / --id / --field N)")
            for v in (a.x, a.y):
                if not (0.0 <= v <= 1.0):
                    fail(f"coordinates are fractions between 0 and 1, not pixels (got {a.x}, {a.y}) — 0.5 0.5 is the middle of the screen; `tree` reports element centers as fractions")
            r = dev_action(did, "input", {"action": "tap", "x": a.x, "y": a.y})
            out(r, "Tapped.")
    elif a.cmd == "swipe":
        for v in (a.x1, a.y1, a.x2, a.y2):
            if not (0.0 <= v <= 1.0):
                fail(f"coordinates are fractions between 0 and 1, not pixels (got {a.x1} {a.y1} -> {a.x2} {a.y2})")
        r = dev_action(did, "input", {"action": "swipe", "x1": a.x1, "y1": a.y1,
                                      "x2": a.x2, "y2": a.y2, "dur": a.ms})
        out(r, "Swiped.")
    elif a.cmd == "key":
        r = dev_action(did, "input", {"action": "key", "key": a.code})
        out(r, "Key sent.")
    elif a.cmd == "text":
        r = dev_action(did, "input", {"action": "text", "text": a.text})
        out(r, "Text typed.")
    elif a.cmd == "shot":
        body = req("GET", f"/portal/api/device/{did}/screenshot", timeout=90, raw=True)
        with open(a.out, "wb") as f:
            f.write(body)
        out({"file": a.out, "bytes": len(body)}, f"Screenshot saved to {a.out} ({len(body)} bytes)")
    elif a.cmd == "handoff":
        r = dev_action(did, "handoff", {"driver": a.driver})
        out(r, f"The wheel now belongs to: {a.driver}")
    elif a.cmd == "relaunch":
        r = dev_action(did, "input", {"action": "launch"})
        out(r, "App relaunched — brought back to the foreground (no reboot).")


if __name__ == "__main__":
    main()
