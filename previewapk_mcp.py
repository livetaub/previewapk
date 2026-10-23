#!/usr/bin/env python3
"""PreviewAPK MCP server — give your AI agent eyes and hands on a real
Android device.

The agent can push an APK build to the owner's PreviewAPK device, start a
preview session, drive the app (tap / swipe / keys / type), take
screenshots it can actually see, hand control to the human, and stop.

Setup:
  1. Download this file:  curl -o previewapk_mcp.py https://previewapk.siteport.site/mcp/previewapk_mcp.py
  2. Get the API key: run `previewapk setup` (the CLI) once on this
     machine — the portal mints the key and the CLI saves it to
     ~/.previewapk/credentials.json, which this server reads when
     PREVIEWAPK_API_KEY is unset — or copy a key from the portal
     (Developers card) and pass it as PREVIEWAPK_API_KEY below.
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
mid-session without rebooting. previewapk_request_human asks the
human for a step only a human should do (passwords, 2FA, payments,
CAPTCHAs) and returns a message to relay verbatim; the handback
reaches you by itself through previewapk_status, the next input
result, or previewapk_wait_for with condition="driver".
previewapk_finish files the session wrap-up (server-composed counts)
and returns the human's summary message.
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

VERSION = "1.1.0"
HOST = os.environ.get("PREVIEWAPK_HOST", "https://previewapk.siteport.site").rstrip("/")
KEY = os.environ.get("PREVIEWAPK_API_KEY", "")
if not KEY:
    # Fall back to the credential `previewapk setup` saved on this
    # machine — one setup covers both clients. The env var still wins.
    try:
        with open(os.path.expanduser("~/.previewapk/credentials.json")) as _f:
            KEY = json.load(_f).get("api_key", "")
    except Exception:
        KEY = ""
DEFAULT_DEVICE = os.environ.get("PREVIEWAPK_DEVICE", "")


class TransportError(Exception):
    """The request or its response died in transit; the server may or
    may not have acted. Verify state — never assume either way."""


class Client:
    """One PreviewAPK API client context: host + key + the working-
    device selection for one agent session."""

    def __init__(self, host=None, key=None, default_device=None,
                 selected_box=None):
        self.host = (HOST if host is None else host).rstrip("/")
        self.key = KEY if key is None else key
        self.default_device = (DEFAULT_DEVICE if default_device is None
                               else default_device)
        # When selected_box (the legacy SELECTED dict) is given, the
        # selection lives in it — the stdio process's exact old
        # behavior. Hosted sessions pass nothing and keep their own.
        self._selected_box = selected_box
        self._selected = ""

    @property
    def selected(self):
        if self._selected_box is not None:
            return self._selected_box["id"]
        return self._selected

    @selected.setter
    def selected(self, value):
        if self._selected_box is not None:
            self._selected_box["id"] = value
        else:
            self._selected = value

    def _api_once(self, method, path, payload, timeout, raw):
        data = None
        headers = {"X-Api-Key": self.key}
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        r = urllib.request.Request(self.host + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(r, timeout=timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as e:
            body = e.read()
            try:
                j = json.loads(body)
                err = j.get("error", f"HTTP {e.code}")
            except Exception:
                j, err = {}, f"HTTP {e.code}"
            # Surface the Phase-1 envelope: stable [code] to branch on, the
            # concrete next step, candidates for ambiguous selectors, and
            # the request id.
            if isinstance(j, dict):
                if j.get("code"):
                    err += f" [{j['code']}]"
                if j.get("next"):
                    err += f" — next: {j['next']}"
                if j.get("candidates"):
                    err += " — candidates: " + json.dumps(j["candidates"])
                if j.get("request_id"):
                    err += f" (request {j['request_id']})"
            raise RuntimeError(err)
        except Exception as e:
            raise TransportError(str(e))
        return body if raw else json.loads(body or b"{}")


    def api(self, method, path, payload=None, timeout=60, raw=False, raise_transport=False):
        if not self.key:
            raise RuntimeError("no PreviewAPK API key — run `previewapk setup` (CLI) once on this machine, or set PREVIEWAPK_API_KEY")
        attempts = 4 if method == "GET" else 1
        for i in range(attempts):
            try:
                return self._api_once(method, path, payload, timeout, raw)
            except TransportError as e:
                if i + 1 < attempts:
                    time.sleep(min(0.7 * (2 ** i), 2.5))
                    continue
                if raise_transport:
                    raise
                raise RuntimeError(f"the connection to PreviewAPK dropped mid-request ({e}) — "
                                   "if this was an action, check the device state before retrying it")


    def pick_device(self, want):
        try:
            state = self.api("GET", "/portal/api/state", raise_transport=True)
        except TransportError:
            raise RuntimeError("the connection to PreviewAPK dropped while checking device state — "
                               "the network cut it repeatedly and nothing was attempted. "
                               "Wait a moment and retry.")
        devs = state.get("devices", [])
        if not devs:
            raise RuntimeError("no devices on this account")
        want = want or self.selected or self.default_device
        if want:
            for d in devs:
                if d["id"] == want:
                    return d, state
            for d in devs:
                if (d.get("name") or "").lower() == want.lower():
                    return d, state
            raise RuntimeError(f"no device '{want}' (have: {', '.join(d.get('name', d['id']) for d in devs)})")
        raise RuntimeError("no working device — call previewapk_list_devices, then select one with previewapk_use_device (or start one with previewapk_start, which selects it). Nothing is guessed: an agent must state where it is working.")


    def act(self, device, action, payload=None):
        return self.api("POST", f"/portal/api/device/{device}/{action}", payload or {})


    def screen_digest(self, device):
        try:
            r = self.api("GET", f"/portal/api/device/{device}/hierarchy",
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


    def act_input(self, device, payload):
        """An input action with a receipt. Returns the server result, or a
        dict saying the connection died and what the state check found —
        {"landed": true} when the screen changed, {"landed": null} when it
        could not be settled. Never a bare connection error."""
        before = self.screen_digest(device)
        try:
            return self.api("POST", f"/portal/api/device/{device}/input", payload, raise_transport=True)
        except TransportError:
            pass
        time.sleep(0.9)
        after = self.screen_digest(device)
        if before is not None and after is not None:
            if after != before:
                return {"ok": True, "landed": True,
                        "note": "the connection dropped mid-call, but the screen changed — the input landed"}
            try:
                return self.api("POST", f"/portal/api/device/{device}/input", payload, raise_transport=True)
            except TransportError:
                pass
        return {"ok": False, "landed": None,
                "note": "the connection dropped mid-call and the state check could not settle whether the input landed — check previewapk_hierarchy before repeating it"}


    def fetch_screenshot(self, device, scale=1.0):
        """Resumable screenshot download (Range + the portal's frame
        cache): a cut transfer picks up at the byte it reached."""
        path = f"/portal/api/device/{device}/screenshot"
        if scale and scale != 1.0:
            path += f"?scale={scale}"
        buf = bytearray()
        total = None
        cap = None
        for _ in range(24):
            headers = {"X-Api-Key": self.key}
            if buf:
                headers["Range"] = f"bytes={len(buf)}-"
            r = urllib.request.Request(self.host + path, headers=headers)
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


    def fetch_binary(self, path):
        """Resumable download of a static payload (a recording MP4, a
        pulled device file) — the same Range discipline as
        fetch_screenshot, without the PNG frame checks. Returns bytes."""
        buf = bytearray()
        total = None
        for _ in range(64):
            headers = {"X-Api-Key": self.key}
            if buf:
                headers["Range"] = f"bytes={len(buf)}-"
            r = urllib.request.Request(self.host + path, headers=headers)
            status, hdrs, chunk = 0, {}, b""
            try:
                with urllib.request.urlopen(r, timeout=120) as resp:
                    status, hdrs = resp.status, resp.headers
                    try:
                        chunk = resp.read()
                    except http.client.IncompleteRead as e:
                        chunk = e.partial or b""
            except urllib.error.HTTPError as e:
                body = e.read()
                try:
                    j = json.loads(body)
                    err = j.get("error", f"HTTP {e.code}")
                    if j.get("code"):
                        err += f" [{j['code']}]"
                    if j.get("next"):
                        err += f" — next: {j['next']}"
                except Exception:
                    err = f"HTTP {e.code}"
                raise RuntimeError(err)
            except Exception:
                time.sleep(0.5)
                continue
            if status == 200 and buf:
                buf, total = bytearray(), None
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
                            continue
                    buf.extend(chunk)
            if total is not None and len(buf) >= total:
                break
            time.sleep(0.3)
        if total is None or len(buf) < total:
            raise RuntimeError("the download could not be completed — the network kept cutting it; call again (transfers resume)")
        return bytes(buf)


    def upload_apk(self, path, device, replace=False):
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
        r = urllib.request.Request(self.host + "/portal/api/upload", data=b"".join(parts), method="POST",
                                   headers={"X-Api-Key": self.key,
                                            "Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(r, timeout=300) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            body = e.read()
            try:
                j = json.loads(body)
                err = j.get("error", "upload failed")
            except Exception:
                j, err = {}, "upload failed"
            if isinstance(j, dict):
                if j.get("code"):
                    err += f" [{j['code']}]"
                if j.get("next"):
                    err += f" — next: {j['next']}"
                if j.get("request_id"):
                    err += f" (request {j['request_id']})"
            raise RuntimeError(err)
        except Exception as e:
            raise TransportError(str(e))


    def call_tool(self, name, args):
        args = args or {}
        want = args.get("device", "")
        if name == "previewapk_push_apk":
            d0, _s0 = self.pick_device(want)
            try:
                res = self.upload_apk(args["path"], d0["id"], replace=bool(args.get("replace")))
            except TransportError:
                try:
                    st = self.api("GET", "/portal/api/state", raise_transport=True)
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
            state = self.api("GET", "/portal/api/state")
            return text_result({"devices": [
                {"id": dv["id"], "name": dv.get("name"), "profile": dv.get("profile"),
                 "android": dv.get("android"), "status": dv.get("status"),
                 "selected": bool(self.selected) and dv["id"] == self.selected,
                 "app": (dv.get("apk") or {}).get("label")} for dv in state.get("devices", [])]})
        if name == "previewapk_use_device":
            d, _s = self.pick_device(args.get("device", ""))
            self.selected = d["id"]
            return text_result({"selected": d.get("name"), "id": d["id"], "status": d.get("status"),
                                "note": "Working device set. Other tools now talk to this device until you select or start another."})
        d, _state = self.pick_device(want)
        did = d["id"]
        if name == "previewapk_status":
            un = upgrade_notice(_state)
            sn = skill_notice(_state)
            extra = {}
            if un:
                extra["client_notice"] = un
            if sn:
                extra["skill_notice"] = sn
            return text_result({**d, **extra} if extra else d)
        if name == "previewapk_start":
            self.selected = did
            r = self.act(did, "run", {"driver": args.get("driver", "agent")})
            links = self.act(did, "links")
            un = upgrade_notice(_state)
            sn = skill_notice(_state)
            return text_result({**r, "links": links, **({"client_notice": un} if un else {}),
                                **({"skill_notice": sn} if sn else {}),
                                "note": "The session takes seconds to boot. Poll previewapk_status until status is 'live', then screenshot. Share the session_url from previewapk_get_links with the human."
                                + "".join(f" Also: {n}" for n in (un, sn) if n)})
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
            return text_result(self.api("POST", "/portal/api/feedback", rep))
        if name == "previewapk_stop":
            r = self.act(did, "stop")
            if _state.get("share_diagnostics") is False:
                return text_result(r)
            return text_result({**r, "note": "Session stopped. You drove it — file a field report while it's fresh with previewapk_feedback: what broke, what worked, what you wish it had. Reports are read and tallied; they decide what gets fixed and built next."})
        if name == "previewapk_get_links":
            return text_result(self.act(did, "links"))
        if name == "previewapk_tap":
            if args.get("ref") is not None:
                if not args.get("snapshot_id"):
                    raise RuntimeError("tap by ref needs the snapshot_id the ref came from (previewapk_observe prints it)")
                return text_result(self.act_input(did, {"action": "tap_ref",
                                                   "snapshot_id": args["snapshot_id"],
                                                   "ref": args["ref"]}))
            if args.get("x") is None or args.get("y") is None:
                raise RuntimeError("tap needs x and y (0..1 fractions) or ref + snapshot_id")
            return text_result(self.act_input(did, {"action": "tap", "x": args["x"], "y": args["y"]}))
        if name == "previewapk_observe":
            r = self.api("POST", f"/portal/api/device/{did}/input",
                    {"action": "observe"}, timeout=90)
            if args.get("screenshot"):
                raw = self.fetch_screenshot(did, args.get("scale", 1.0))
                return {"content": [
                    {"type": "text", "text": json.dumps(r, indent=1)},
                    {"type": "image", "data": base64.b64encode(raw).decode(),
                     "mimeType": "image/png"}]}
            return text_result(r)
        if name == "previewapk_act":
            # Batches are never auto-replayed: self.api() makes one attempt for
            # POSTs, and a transport failure raises with the honest
            # check-state-first message.
            r = self.api("POST", f"/portal/api/device/{did}/input",
                    {"action": "act", "actions": args.get("actions") or [],
                     "observe": args.get("observe", "none")}, timeout=330)
            b64 = r.pop("screenshot_b64", None)
            if b64:
                return {"content": [
                    {"type": "text", "text": json.dumps(r, indent=1)},
                    {"type": "image", "data": b64, "mimeType": "image/png"}]}
            return text_result(r)
        if name == "previewapk_wait_for":
            if args.get("condition") == "driver":
                # Handback wait (2026-10-23): park until the human
                # hands the wheel back. Client-side polling of state —
                # the poll that first sees driver=agent is the read
                # the server delivers the handback note on, so the
                # note rides this result.
                timeout_s = min(600.0, float(args.get("timeout_s", 300)))
                t0 = time.time()
                while True:
                    waited = int((time.time() - t0) * 1000)
                    st = self.api("GET", "/portal/api/state")
                    dv = next((x for x in st.get("devices", [])
                               if x["id"] == did), None)
                    if dv is None:
                        return text_result({"found": False,
                                            "waited_ms": waited,
                                            "reason": "device_gone"})
                    if dv.get("status") != "live":
                        return text_result({"found": False,
                                            "waited_ms": waited,
                                            "reason": "session_ended",
                                            "status": dv.get("status")})
                    if dv.get("driver") == "agent":
                        out = {"found": True, "waited_ms": waited,
                               "driver": "agent",
                               "driver_note": dv.get("driver_note"),
                               "handoff": dv.get("handoff")}
                        if dv.get("resumed"):
                            out["resumed"] = True
                            out["handback_note"] = dv.get(
                                "handback_note") or ""
                            out["handback_at"] = dv.get("handback_at")
                        return text_result(out)
                    if time.time() - t0 >= timeout_s:
                        return text_result({"found": False,
                                            "waited_ms": waited,
                                            "driver": dv.get("driver"),
                                            "handoff": dv.get("handoff")})
                    time.sleep(3)
            payload = {"action": "wait_for",
                       "present": args.get("present", True),
                       "timeout_s": args.get("timeout_s", 30)}
            for k in ("text", "id", "desc"):
                if args.get(k):
                    payload[k] = args[k]
            if len(payload) <= 3:
                raise RuntimeError("wait_for needs a condition ('driver') or a text, id, or desc to wait for")
            return text_result(self.api("POST", f"/portal/api/device/{did}/input",
                                   payload,
                                   timeout=min(240, int(payload["timeout_s"]) + 60)))
        if name == "previewapk_record_start":
            return text_result(self.api("POST", f"/portal/api/device/{did}/input",
                                   {"action": "record_start",
                                    "name": args.get("name", "")}, timeout=90))
        if name == "previewapk_record_stop":
            return text_result(self.api("POST", f"/portal/api/device/{did}/input",
                                   {"action": "record_stop"}, timeout=330))
        if name == "previewapk_record_download":
            rec_id = args.get("rec_id")
            if not rec_id:
                st = self.api("POST", f"/portal/api/device/{did}/input",
                         {"action": "record_status"}, timeout=330)
                if st.get("recording"):
                    raise RuntimeError("a recording is still running — stop it with previewapk_record_stop first")
                last = st.get("last")
                if not last:
                    raise RuntimeError("no finished recording for this session yet — record_start, drive, record_stop, then download")
                rec_id = last["rec_id"]
            data = self.fetch_binary(f"/portal/api/device/{did}/recording/{rec_id}")
            with open(args["path"], "wb") as f:
                f.write(data)
            return text_result({"saved": args["path"], "rec_id": rec_id,
                                "bytes": len(data)})
        if name == "previewapk_device":
            a2 = args.get("action")
            base = {"action": "device"}
            if a2 == "set_timezone":
                pl, to = {**base, "op": "timezone", "tz": args.get("tz", "")}, 150
            elif a2 == "set_locale":
                pl = {**base, "op": "locale", "locale": args.get("locale", "")}
                if args.get("package"):
                    pl["package"] = args["package"]
                to = 150
            elif a2 == "set_location":
                pl = {**base, "op": "location", "lat": args.get("lat"),
                      "lon": args.get("lon")}
                to = 150
            elif a2 == "clear_location":
                pl, to = {**base, "op": "location_clear"}, 150
            elif a2 == "list_files":
                pl = {**base, "op": "file_list",
                      "path": args.get("remote_path", "Download")}
                to = 150
            elif a2 == "delete_file":
                pl = {**base, "op": "file_delete",
                      "path": args.get("remote_path", "")}
                to = 150
            elif a2 == "reboot":
                pl, to = {**base, "op": "reboot"}, 300
            elif a2 == "push_file":
                lp = args.get("local_path", "")
                with open(lp, "rb") as f:
                    raw = f.read()
                if len(raw) > 10 * 1024 * 1024:
                    raise RuntimeError(f"{lp} is {len(raw)} bytes — device file pushes cap at 10 MB")
                remote = args.get("remote_path") or ("Download/" + os.path.basename(lp))
                pl = {**base, "op": "file_push", "path": remote,
                      "data_b64": base64.b64encode(raw).decode()}
                to = 240
            elif a2 == "pull_file":
                import urllib.parse as _up2
                data = self.fetch_binary(f"/portal/api/device/{did}/file?path="
                                    + _up2.quote(args.get("remote_path", "")))
                with open(args["local_path"], "wb") as f:
                    f.write(data)
                return text_result({"saved": args["local_path"],
                                    "remote": args.get("remote_path"),
                                    "bytes": len(data)})
            else:
                raise RuntimeError(f"unknown device action {a2!r}")
            return text_result(self.api("POST", f"/portal/api/device/{did}/input",
                                   pl, timeout=to))
        if name == "previewapk_swipe":
            return text_result(self.act_input(did, {"action": "swipe", "x1": args["x1"], "y1": args["y1"],
                                                  "x2": args["x2"], "y2": args["y2"],
                                                  "dur": args.get("duration_ms", 300)}))
        if name == "previewapk_press_key":
            return text_result(self.act_input(did, {"action": "key", "key": args["keycode"]}))
        if name == "previewapk_type_text":
            return text_result(self.act_input(did, {"action": "text", "text": args["text"]}))
        if name == "previewapk_relaunch":
            return text_result(self.act_input(did, {"action": "launch"}))
        if name == "previewapk_screenshot":
            raw = self.fetch_screenshot(did, args.get("scale", 1.0))
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
            r = self.api("GET", f"/portal/api/device/{did}/logs?" + _up.urlencode(params), timeout=60)
            shown = [e for e in r.get("lines", [])
                     if not (args.get("errors_only") and e.get("env"))]
            body = "\n".join(e["raw"] for e in shown) or "(no log lines matched)"
            head = f"{len(shown) if args.get('errors_only') else r.get('count', 0)} line(s) from {d.get('name')}"
            if r.get("ended"):
                head += " — session ended, saved tail of the last session"
            return {"content": [{"type": "text", "text": head + "\n" + body}]}
        if name == "previewapk_hierarchy":
            r = self.api("GET", f"/portal/api/device/{did}/hierarchy", timeout=90)
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
            r = self.api("GET", f"/portal/api/device/{did}/hierarchy", timeout=90)
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
            if len(ms) > 1 and args.get("index") is None:
                # Ambiguous at the same match level: hand back the
                # candidates instead of silently picking one (Phase 1).
                return text_result({
                    "ok": False,
                    "error": f"{len(ms)} elements match '{want_text}' — refusing to guess",
                    "code": "ambiguous_target", "retryable": False,
                    "next": "call again with index (0-based, in the order listed)",
                    "candidates": [{"text": n.get("text"), "desc": n.get("desc"),
                                    "id": n.get("id"), "class": n.get("class"),
                                    "center": n.get("center")} for n in ms]})
            idx = int(args.get("index") or 0)
            if idx >= len(ms):
                raise RuntimeError(f"index {idx} out of range ({len(ms)} match(es))")
            n = ms[idx]
            if "center" not in n:
                raise RuntimeError("matched element has no bounds to tap")
            res = self.act_input(did, {"action": "tap", "x": n["center"][0], "y": n["center"][1]})
            return text_result({**res, "tapped": {"text": n.get("text"), "class": n.get("class"),
                                                 "center": n["center"], "matches": len(ms)}})
        if name == "previewapk_handoff":
            return text_result(self.act(did, "handoff", {"driver": args["to"]}))
        if name == "previewapk_request_human":
            reason = " ".join(str(args.get("reason") or "").split())[:200]
            if not reason:
                raise RuntimeError("request_human needs a reason — one short line saying what the human needs to do")
            return text_result(self.act(did, "request-human",
                                        {"reason": reason}))
        if name == "previewapk_finish":
            summary = " ".join(str(args.get("summary") or "").split())[:1000]
            if not summary:
                raise RuntimeError("finish needs a summary — what you tested or checked, in your own words")
            payload = {"summary": summary,
                       "end_session": bool(args.get("end_session"))}
            if args.get("verdict"):
                payload["verdict"] = args["verdict"]
            return text_result(self.act(did, "finish", payload))
        raise RuntimeError(f"unknown tool {name}")


SELECTED = {"id": ""}  # this agent session's working device


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


def skill_notice(state):
    """One factual line naming the current published skill version,
    from live state. The client cannot know which skill copy the
    agent loaded, so this never claims staleness — the agent (and
    the skill's own self-refresh section) does the comparing.
    Silent when the server doesn't advertise a skill (old server)."""
    info = ((state or {}).get("clients") or {}).get("skill") or {}
    ver, url = info.get("version"), info.get("url")
    if ver and url:
        return (f"Skill file: the current PreviewAPK skill is v{ver} at {url}; "
                f"if your loaded skill is older, re-fetch it.")
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
     "description": "State of the owner's PreviewAPK device(s): attached app, session status, driver, links. Also carries the handoff block (a pending request for the human: reason + when), the agent's finish_report once filed, and — exactly once, on the first status read after the human hands the wheel back — handback_note + resumed:true: that is how you learn the handback happened, no chat message needed.",
     "inputSchema": {"type": "object", "properties": {"device": {"type": "string", "description": "device name or id (optional)"}}}},
    {"name": "previewapk_push_apk",
     "description": "Upload an APK file from this machine to the device. If the device is OFF it becomes the attached app (then call previewapk_start). If the device is RUNNING and you hold the wheel, the new app is hot-swapped in live — installed and relaunched with no reboot. Returns the parsed app info (swapped=true on a hot swap; deduped=true when identical bytes were already stored). Installing an OLDER versionCode over the app in a live session is refused (version_downgrade) unless replace=true, which uninstalls the installed app first — its data is deleted.",
     "inputSchema": {"type": "object", "required": ["path"], "properties": {
         "path": {"type": "string", "description": "local path to the .apk file"},
         "replace": {"type": "boolean", "description": "allow replacing a newer installed build with an older one (uninstalls first, deleting its data)"},
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
     "description": "Tap the screen. Either coordinates (0..1 fractions of the screen, resolution-independent) or a ref from previewapk_observe: pass ref + snapshot_id and the server re-finds that exact element on the current screen first — if it moved or changed, the call fails with stale_snapshot instead of tapping the wrong thing. Requires an agent-driven session.",
     "inputSchema": {"type": "object", "properties": {
         "x": {"type": "number"}, "y": {"type": "number"},
         "ref": {"type": "integer", "description": "element number from a previewapk_observe snapshot"},
         "snapshot_id": {"type": "string", "description": "the snapshot_id that ref belongs to"},
         "device": {"type": "string"}}}},
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
     "description": "Capture the device's current screen as an image you can see (the real screen — it agrees with the hierarchy). Use it to verify your changes look right. The download resumes automatically if the connection is cut; pass scale=0.5 on weak networks for a quarter-size transfer.",
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
     "description": "Tap the element whose visible text matches (exact match preferred, else substring; use index to pick among matches). If several elements match at the same level and no index is given, the call returns an ambiguous_target result with the candidates instead of guessing. Resolves the element's center from the UI hierarchy — prefer this over coordinate taps whenever the target has text.",
     "inputSchema": {"type": "object", "required": ["text"], "properties": {
         "text": {"type": "string"}, "index": {"type": "integer"}, "device": {"type": "string"}}}},
    {"name": "previewapk_observe",
     "description": "Read the current screen as a compact numbered snapshot: foreground app, keyboard state, screen size, and the useful elements (ref, type, text, desc, id, pixel bounds/center, state flags) in reading order — plus the same snapshot as compact text. Much cheaper than a screenshot and cheaper than the full hierarchy. Keep the snapshot_id: previewapk_tap with ref + snapshot_id taps an element from it safely (stale_snapshot if the screen changed). Set screenshot=true to also get the frame as an image.",
     "inputSchema": {"type": "object", "properties": {
         "screenshot": {"type": "boolean", "default": False},
         "scale": {"type": "number", "description": "screenshot scale, e.g. 0.5 (only with screenshot=true)"},
         "device": {"type": "string"}}}},
    {"name": "previewapk_act",
     "description": "Run a batch of up to 20 actions in order on the device: tap (x/y fractions, or target {text|id|desc, nth}, or target {ref, snapshot_id}), swipe {x1,y1,x2,y2}, long_press, double_tap, type {text, clear, submit}, key {key: back|home|recents|enter|delete or code}, open_app {package}, close_app {package}, open_url {url}, notifications, wait {ms}, wait_for {text|id|desc, present, timeout_s}. Stops at the first failure — earlier actions stay done — and returns per-action results with timing and error envelopes. NEVER replay a batch whose outcome you don't know; observe first. observe='ui'|'screenshot'|'both' attaches the screen after the batch.",
     "inputSchema": {"type": "object", "required": ["actions"], "properties": {
         "actions": {"type": "array", "items": {"type": "object"}, "maxItems": 20},
         "observe": {"type": "string", "enum": ["none", "ui", "screenshot", "both"], "default": "none"},
         "device": {"type": "string"}}}},
    {"name": "previewapk_wait_for",
     "description": "Wait until an element is present on screen (or gone, with present=false), polling the UI. Returns {found, waited_ms} — found=false at the timeout is a result, not an error. Use after actions that load content instead of sleeping blind. With condition='driver' instead of an element: wait until the human hands the wheel back to you (after previewapk_request_human or any takeover) — the result carries the handback note (handback_note, resumed) when there is one. Driver waits default to timeout_s=300 (max 600): a human step takes human time.",
     "inputSchema": {"type": "object", "properties": {
         "condition": {"type": "string", "enum": ["element", "driver"], "default": "element", "description": "'driver' waits for the handback instead of an element"},
         "text": {"type": "string"}, "id": {"type": "string"}, "desc": {"type": "string"},
         "present": {"type": "boolean", "default": True},
         "timeout_s": {"type": "number", "default": 30},
         "device": {"type": "string"}}}},
    {"name": "previewapk_record_start",
     "description": "Start recording the device screen to an MP4 (screenrecord; one recording caps at 180s). Stop it with previewapk_record_stop, then download with previewapk_record_download. A recording in progress is stopped and saved automatically if the session ends or the phone reboots.",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string", "description": "a label for the recording"},
         "device": {"type": "string"}}}},
    {"name": "previewapk_record_stop",
     "description": "Stop the running recording. The MP4 is pulled off the device to the server and indexed (rec_id, size, duration) — download it with previewapk_record_download. Recordings stay downloadable after the session ends.",
     "inputSchema": {"type": "object", "properties": {"device": {"type": "string"}}}},
    {"name": "previewapk_record_download",
     "description": "Download a finished recording's MP4 to a local file path on this machine (resumable — a cut transfer picks up where it stopped). Omit rec_id to get the session's latest finished recording.",
     "inputSchema": {"type": "object", "required": ["path"], "properties": {
         "path": {"type": "string", "description": "local file path to save the MP4 to"},
         "rec_id": {"type": "string", "description": "from previewapk_record_stop (default: latest)"},
         "device": {"type": "string"}}}},
    {"name": "previewapk_request_human",
     "description": "Ask the human for a step only a human should do: a password or 2FA code typed on the phone itself, a payment or consent approval, a CAPTCHA, anything sensitive or irreversible. Records the request in the portal (the device card shows 'Your agent needs you') and returns message_for_human — relay it to the human VERBATIM in chat, then park on previewapk_wait_for with condition='driver' until the wheel comes back (the handback note rides that result). Never ask the human to paste a password or code into chat for you to type — the human types it on the phone. Requires a live session you are driving.",
     "inputSchema": {"type": "object", "required": ["reason"], "properties": {
         "reason": {"type": "string", "description": "one short line: what the human needs to do (max ~200 chars)"},
         "device": {"type": "string"}}}},
    {"name": "previewapk_finish",
     "description": "File the wrap-up when your work on this session is done, and get back message_for_human — relay it to the human VERBATIM. The counts in it (session duration, actions taken, errors) are composed by the SERVER from the session's real record — never state counts from your own memory instead. The report is also stored on the session (visible in the portal, kept in the session archive after the session ends). end_session defaults to FALSE: finishing does not stop the phone — it stays live at the portal link and idle-stops on the normal 15-minute rule. Pass end_session=true only when the human should get the session-record wording and the phone should stop now. File your previewapk_feedback field report too — finish is the human's summary, feedback is the tool team's.",
     "inputSchema": {"type": "object", "required": ["summary"], "properties": {
         "summary": {"type": "string", "description": "what you tested or checked, in your own words (max ~1000 chars)"},
         "verdict": {"type": "string", "enum": ["passed", "failed", "mixed"], "description": "optional overall verdict"},
         "end_session": {"type": "boolean", "default": False, "description": "also stop the session after filing (default false)"},
         "device": {"type": "string"}}}},
    {"name": "previewapk_device",
     "description": "Device controls for realistic QA. action='set_timezone' (IANA tz, e.g. America/New_York) — verified on the device. action='set_locale' (BCP 47 tag, e.g. de-DE; package optional, defaults to the session app) — per-app language, Android 13+ style; relaunch the app if it doesn't switch live. action='set_location' (lat, lon) — GPS reports a mock fix until cleared. action='clear_location'. action='push_file' (local_path on this machine, remote_path under /sdcard, max 10 MB) — test fixtures, seed files. action='pull_file' (remote_path, local_path, max 10 MB). action='list_files' (remote_path folder, default Download). action='delete_file' (remote_path). action='reboot' — restarts the phone (same phone, same data: app, files and settings survive; ~a minute; a running recording is saved first). There is NO clipboard control: not available on these phones.",
     "inputSchema": {"type": "object", "required": ["action"], "properties": {
         "action": {"type": "string", "enum": ["set_timezone", "set_locale", "set_location", "clear_location", "push_file", "pull_file", "list_files", "delete_file", "reboot"]},
         "tz": {"type": "string", "description": "IANA timezone (set_timezone)"},
         "locale": {"type": "string", "description": "BCP 47 tag (set_locale)"},
         "package": {"type": "string", "description": "app package (set_locale; default: session app)"},
         "lat": {"type": "number"}, "lon": {"type": "number"},
         "local_path": {"type": "string", "description": "file on this machine (push_file: source; pull_file: destination)"},
         "remote_path": {"type": "string", "description": "path on the device, under /sdcard (e.g. Download/fixture.json)"},
         "device": {"type": "string"}}}},
]


# The process-wide default client — what the stdio server below runs
# on (env key/host, credentials-file fallback, selection backed by
# SELECTED exactly as before the Client refactor).
_default_client = Client(selected_box=SELECTED)


def api(method, path, payload=None, timeout=60, raw=False, raise_transport=False):
    return _default_client.api(method, path, payload, timeout, raw, raise_transport)


def pick_device(want):
    return _default_client.pick_device(want)


def act(device, action, payload=None):
    return _default_client.act(device, action, payload)


def screen_digest(device):
    return _default_client.screen_digest(device)


def act_input(device, payload):
    return _default_client.act_input(device, payload)


def fetch_screenshot(device, scale=1.0):
    return _default_client.fetch_screenshot(device, scale)


def fetch_binary(path):
    return _default_client.fetch_binary(path)


def upload_apk(path, device, replace=False):
    return _default_client.upload_apk(path, device, replace)


def call_tool(name, args):
    return _default_client.call_tool(name, args)


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

