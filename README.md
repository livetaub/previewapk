# PreviewAPK — a phone for your AI agent

Your agent builds an Android app, and then… neither of you can see it run.
No emulator on the machine, no device on the desk, and the Play Store is
not a feedback loop.

**PreviewAPK gives your agent a real cloud Android phone.** Push an APK,
it boots in the browser in seconds. The agent drives it — taps,
types, screenshots, logs — and verifies its own work before you ever look.
You watch the same phone through a link, take the wheel whenever you want,
and every session gets a share link you can hand to a client
or paste into a PR.

- $19/month per device, unlimited runs. One device runs one app at a
  time; swap apps freely.
- Links are view-only, always. Only the owner's key (agent) or the owner
  in the portal drives — one driver per session, switchable mid-session
  with an explicit handoff.
- No Google Play services on the phones. Apps that need Google sign-in,
  Maps, or FCM will hit that wall; everything else just runs.

This repo is the **open client**: the CLI and the MCP server (both
single-file, Python standard library only). The PreviewAPK service itself
is hosted — you bring an API key from your account at
[previewapk.siteport.site](https://previewapk.siteport.site).
PreviewAPK is currently in waitlist beta; join there.

## Quick start — CLI

```sh
curl -O https://previewapk.siteport.site/cli/previewapk.py
export PREVIEWAPK_API_KEY=pvk_…        # from your portal's Developers card

python3 previewapk.py devices          # see your devices
python3 previewapk.py use "Device 1"   # pick the working device (like cd)
python3 previewapk.py push app.apk --run
python3 previewapk.py tree             # the screen as an element tree
python3 previewapk.py tap --text "Log in"
python3 previewapk.py text "demo@example.com"
python3 previewapk.py shot             # a screenshot you can open
python3 previewapk.py logs --errors    # the app's logcat, noise filtered
python3 previewapk.py relaunch         # app back in front, ~2s, no reboot
python3 previewapk.py handoff user     # hand the human the wheel
python3 previewapk.py links            # the share link for the live session
```

Pushing to a **running** device hot-swaps the app in — installed and
relaunched in the same session, no reboot.

Coordinates are fractions (0–1), never pixels; the CLI and API reject
out-of-range values instead of silently tapping the wrong place, and
`text` refuses when no field is focused instead of typing into the void.
Signals an agent can trust: errors say what happened and what to do
instead.

## Quick start — MCP server

Download `previewapk_mcp.py` from this repo, then register it with your
MCP client. Claude Code:

```sh
claude mcp add previewapk \
  --env PREVIEWAPK_API_KEY=pvk_… \
  -- python3 /path/to/previewapk_mcp.py
```

Cursor / Claude Desktop / other MCP clients (JSON config):

```json
{
  "mcpServers": {
    "previewapk": {
      "command": "python3",
      "args": ["/path/to/previewapk_mcp.py"],
      "env": { "PREVIEWAPK_API_KEY": "pvk_…" }
    }
  }
}
```

17 tools: list/use devices, push (hot-swaps a running device), start,
stop, links, tap by coordinates or by label, swipe, key, type text,
relaunch, screenshot (returns a real image, from the framebuffer — it
agrees with the hierarchy), UI hierarchy, session logs, handoff.

The working-device model mirrors the CLI: an agent session selects a
device once (`previewapk_use_device` or `previewapk_start`), then works
on it without repeating its name. Nothing has a default device — an
action with no device selected refuses, so a build can never land on the
wrong phone by assumption.

## How agents should drive

Navigate and assert with the **hierarchy** (element tree: class, label,
center, state flags) — fast, cheap, exact. Verify **visuals** with a
screenshot. Read the **logs** after actions; a crash's exact reason lives
there. Emulator noise (graphics-stack spam from the virtual GPU) is
tagged and kept out of errors-only views; copies and downloads are always
the full raw log.

Full documentation: [previewapk.siteport.site/docs](https://previewapk.siteport.site/docs/)
(guides for the [API](https://previewapk.siteport.site/docs/api/),
[CLI](https://previewapk.siteport.site/docs/cli/),
[MCP](https://previewapk.siteport.site/docs/mcp/), and
[CI recipes](https://previewapk.siteport.site/docs/ci/)).
Agent briefing: [previewapk.siteport.site/agents](https://previewapk.siteport.site/agents)

## License

The client tools in this repo are MIT-licensed (see `LICENSE`). The
PreviewAPK service is a separate, hosted product.
