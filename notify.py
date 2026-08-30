#!/usr/bin/env python3
"""Herdr -> Warp desktop notifications.

Herdr already knows when an agent finishes or needs you: it fires
`pane.agent_status_changed` and plays a sound. What it does not do is get a
desktop notification out of Warp, for two independent reasons:

  1. Format. Herdr's `[ui.toast] delivery = "terminal"` emits OSC 9 and OSC 99
     (kitty) terminated with ST (ESC \\). Warp's notification parser wants a BEL
     terminator, and its own agent integration uses OSC 777 (`]777;notify;t;b`).
  2. Scope. Herdr only toasts *background* workspaces. The workspace you are
     looking at never notifies -- even when you have tabbed away from Warp
     entirely, which is exactly when you want to be told.

This hook fixes both: it emits OSC 777 + BEL straight to the tty the herdr
client is attached to (Warp's PTY), for every attention-worthy transition
regardless of which workspace herdr thinks is focused. Warp only raises a
desktop notification while Warp is unfocused, so Warp is the focus gate.

When you *are* looking at Herdr, Warp stays silent -- so the same message also
goes to Herdr's in-app toast via `notification.show`, which Herdr draws in the
active theme (`[theme] name` / `auto_switch`). That covers the workspace you are
looking at, which Herdr's own toast deliberately skips; background workspaces
still toast themselves, so no event ever produces two. Needs
`[ui.toast] delivery = "herdr"`, the only delivery that draws in-app, and it is
held open for about twice Herdr's own dwell -- see the knobs below.

Run `python3 notify.py --self-check` for the offline assertions, or
`python3 notify.py --test` to fire one real notification.
"""

import json
import os
import socket
import subprocess
import sys
import time

# Herdr's two notification-worthy states, matching its own sound categories
# (`request_path` = needs-attention, `done_path` = finished). The glyph is the
# only colour either surface gives us -- a macOS banner and a Herdr toast are
# both flat text in the host's own palette -- so it carries the state at a
# glance and is the same on both.
STATES = {
    "blocked": ("🔔", "needs you"),
    "done": ("✅", "finished"),
}

# Herdr 0.8 has no toast dwell setting -- `[ui.toast]` carries only `delivery`
# and `delay_seconds` (a debounce *before* notifying). Its own dwell measures
# ~3.3s: a second `notification.show` is refused with reason "busy" until the
# first expires, and it will not refresh a live toast. So the only way to hold
# one up longer is to re-show it the instant Herdr frees the slot. Each cycle
# buys another full dwell, so 1 cycle is roughly double -- there is no half
# cycle, and 2x is the closest reachable step up from 1x.
#
# ponytail: calibration knob, not arithmetic. Herdr's dwell is internal and
# will drift between versions; DWELL just has to land shortly *before* it, so
# the poll that follows catches the hand-off within one interval. Set
# HOLD_CYCLES = 0 to leave Herdr's own timing alone.
TOAST_HOLD_CYCLES = 1
TOAST_DWELL_SECONDS = 2.5
TOAST_POLL_SECONDS = 0.1
TOAST_POLL_LIMIT = 3.0

# `herdr session attach <name>` is the only client form that leads with a word.
CLIENT_SUBCOMMANDS = ("session",)


def sanitize(text, limit=200):
    """Warp's OSC 777 payload is semicolon-delimited and single-line."""
    if not text:
        return ""
    out = " ".join(str(text).split()).replace(";", ",")
    return out[: limit - 1] + "…" if len(out) > limit else out


def osc777(title, body):
    return "\033]777;notify;%s;%s\007" % (title, body)


def _ps(fmt, pid):
    try:
        out = subprocess.run(["ps", "-o", fmt, "-p", str(pid)],
                             capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return out.split()


def _is_client_argv(argv, binname):
    """A herdr *client* is a bare `herdr` invocation, never a subcommand.

    Excludes `herdr server` (headless, no tty) and things a user might type in a
    pane, which do have a tty -- `herdr status`, `herdr pane list`, ...
    """
    if not argv or os.path.basename(argv[0]) != binname:
        return False
    if len(argv) == 1:
        return True
    # Clients are `herdr`, `herdr --session x`, `herdr --remote host`, or
    # `herdr session attach x`. Anything else leading with a bare word is a
    # one-shot subcommand -- `herdr server`, or `herdr status` typed in a pane.
    return argv[1].startswith("-") or argv[1] in CLIENT_SUBCOMMANDS


def client_ttys():
    """Ttys of the herdr client(s) -- the outer terminal, i.e. the one Warp owns.

    Found by locating the herdr client process. Empty when no client is
    attached -- e.g. a detached session, or between a live handoff and a
    reattach -- which is correct: there is no outer terminal to notify.
    """
    binname = os.path.basename(os.environ.get("HERDR_BIN_PATH") or "herdr")
    found = []
    try:
        out = subprocess.run(["ps", "-eo", "tty=,args="],
                             capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        tty, args = parts
        if tty in ("??", "-", "?"):
            continue
        if _is_client_argv(args.split(), binname):
            dev = "/dev/" + tty
            if dev not in found:
                found.append(dev)
    # ponytail: notifies every attached client. Fine for one Warp window; if you
    # attach several, filter by tty here.
    #
    # No client attached -> no outer terminal to notify, so return nothing. Do
    # NOT fall back to walking our own ancestry: that resolves to a
    # herdr-managed *pane* PTY, and herdr swallows OSC 777 arriving from panes.
    return found


def emit(title, body):
    seq = osc777(title, body).encode()
    sent = 0
    for dev in client_ttys():
        try:
            # One write() so it cannot interleave with the client's rendering.
            fd = os.open(dev, os.O_WRONLY | os.O_NOCTTY)
        except OSError:
            continue
        try:
            os.write(fd, seq)
            sent += 1
        except OSError:
            pass
        finally:
            os.close(fd)
    return sent


def _call(method, params):
    path = os.environ.get("HERDR_SOCKET_PATH")
    if not path:
        return None
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(1.0)
        client.connect(path)
        req = {"id": "herdr-warp", "method": method, "params": params}
        client.sendall((json.dumps(req) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = client.recv(65536)
            if not chunk:
                break
            buf += chunk
        client.close()
        return json.loads(buf.split(b"\n", 1)[0]) if buf else None
    except Exception:
        return None


def toast_delivery():
    """`[ui.toast] delivery` -- "off" | "herdr" | "terminal" | "system".

    Herdr's default is "off". No env var carries the value, and system python is
    3.9 with no tomllib, so scan the file.
    """
    # ponytail: naive line scan, enough for an enum key in a known section.
    path = os.environ.get("HERDR_CONFIG_PATH") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or "~/.config", "herdr", "config.toml")
    path = os.path.expanduser(path)
    section = ""
    try:
        with open(path) as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if line.startswith("["):
                    section = line.strip("[]").strip()
                elif section == "ui.toast" and line.startswith("delivery"):
                    return line.split("=", 1)[1].strip().strip("'\"")
    except OSError:
        pass
    return "off"


def _show(params):
    """notification.show -> "shown" | "busy" | "disabled" | ... | None."""
    reply = _call("notification.show", params)
    try:
        return reply["result"]["reason"]
    except (TypeError, KeyError):
        return None


def toast(title, body):
    """Herdr's own in-app popup, same two strings, drawn in the active theme.

    Herdr owns the colours here -- passing our own would ignore whichever theme
    is live (and `auto_switch` flips it under us), so we pass text only.

    Then holds it up for TOAST_HOLD_CYCLES extra dwells (see the knobs at the
    top). Returns the first reason ("shown", "disabled", "busy", ...) or None;
    the hold is best-effort and never changes it.
    """
    if toast_delivery() != "herdr":
        return None
    # sound="none": `[ui.sound]` already plays the state's sound on this event.
    params = {"title": title, "body": body, "sound": "none"}
    reason = _show(params)
    if reason != "shown":
        return reason
    for _ in range(TOAST_HOLD_CYCLES):
        time.sleep(TOAST_DWELL_SECONDS)     # nearly expired by now
        # One call both probes and re-shows: "busy" means the old toast is still
        # up, anything else means this call replaced it. Polling instead of
        # sleeping blind keeps the seam to one interval, and starting the poll
        # late keeps it to a handful of API calls rather than thirty.
        waited = 0.0
        while _show(params) == "busy" and waited < TOAST_POLL_LIMIT:
            time.sleep(TOAST_POLL_SECONDS)
            waited += TOAST_POLL_SECONDS
    return reason


def locate(pane_id, workspace_id):
    """(workspace label, tab label, pane title, workspace is focused).

    A Warp notification click can only reach the Warp tab hosting the herdr
    client -- Warp sees one PTY for every herdr pane -- so naming the workspace
    and tab is what actually gets the reader to the right place. Herdr's own
    `prefix+o` (open_notification_target) jumps there directly.

    The focus flag is what keeps the in-app toast from doubling up: Herdr toasts
    background workspaces itself, and only those.
    """
    reply = _call("session.snapshot", {})
    try:
        snap = reply["result"]["snapshot"]
    except (TypeError, KeyError):
        return workspace_id or "", "", "", False

    ws_label, tab_id, pane_title = workspace_id or "", None, ""
    for ws in snap.get("workspaces", []):
        if ws.get("workspace_id") == workspace_id:
            ws_label = ws.get("label") or ws_label
            break
    for pane in snap.get("agents", []) + snap.get("panes", []):
        if pane.get("pane_id") == pane_id:
            tab_id = pane.get("tab_id")
            pane_title = (pane.get("terminal_title_stripped")
                          or pane.get("label") or "")
            break
    tab_label = ""
    if tab_id:
        for tab in snap.get("tabs", []):
            if tab.get("tab_id") == tab_id:
                tab_label = tab.get("label") or ""
                break
    return (ws_label, tab_label, pane_title,
            snap.get("focused_workspace_id") == workspace_id)


def compose(event, ws_label, tab_label="", pane_title=""):
    """(title, body) for an event, or None if this event is not worth a toast.

    Title says who wants what -- "🔔 Claude needs you". Body says where to go --
    "portal › WEB-1204 › add text to loading page". Both surfaces get the same
    two strings: macOS renders the title bold above the body, and Herdr's
    in-app toast stacks them the same way, so "which space, which pane" reads
    identically whether or not you are looking at Herdr.
    """
    status = event.get("agent_status")
    if status not in STATES:
        return None
    glyph, fallback = STATES[status]
    # Real events often omit display_agent, leaving a bare id like "claude".
    agent = event.get("display_agent") or ""
    if not agent:
        agent = (event.get("agent") or "Agent").replace("-", " ").title()
    # Herdr's own wording for the state when it has one, else our fallback.
    state = (event.get("state_labels") or {}).get(status) or fallback
    title = sanitize("%s %s %s" % (glyph, agent, state), 80)

    # Body is the route back: workspace › tab › what the agent is actually on.
    # Herdr names tabs "1", "2", ... until you rename them; a bare number is
    # noise in a notification, so drop it.
    if tab_label.strip().isdigit():
        tab_label = ""
    where = []
    for part in (sanitize(tab_label, 40),
                 sanitize(event.get("title") or pane_title, 120)):
        if part and part not in where:
            where.append(part)
    # A tab label is often a prefix of the pane title; keep only the longer one.
    if len(where) == 2 and where[1].startswith(where[0]):
        where = [where[1]]
    ws = sanitize(ws_label, 40)
    if ws and ws not in where:
        where.insert(0, ws)
    return title, " › ".join(where) or "Open Herdr to pick it up"


def unwrap(raw):
    """Herdr delivers `{"event": "...", "data": {...}}`; accept a bare payload too."""
    try:
        event = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(event, dict):
        return None
    if "agent_status" not in event and isinstance(event.get("data"), dict):
        return event["data"]
    return event


def trace(msg):
    """Opt-in breadcrumbs: `touch $HERDR_PLUGIN_STATE_DIR/debug` to enable.

    An event hook is fire-and-forget with nowhere to print, so this is the only
    way to see what it did.
    """
    for var in ("HERDR_PLUGIN_STATE_DIR", "HERDR_PLUGIN_CONFIG_DIR"):
        d = os.environ.get(var)
        if not d or not os.path.exists(os.path.join(d, "debug")):
            continue
        try:
            with open(os.path.join(d, "debug.log"), "a") as fh:
                fh.write("%s %s\n" % (int(time.time()), msg))
        except OSError:
            pass
        return


def main():
    raw = os.environ.get("HERDR_PLUGIN_EVENT_JSON")
    trace("fired: %s" % (raw or "<no event json>"))
    if not raw:
        return 0
    event = unwrap(raw)
    if event is None:
        return 0

    if not compose(event, ""):
        return 0
    # Only pay for the socket round-trip once we know we are notifying.
    ws_label, tab_label, pane_title, focused = locate(event.get("pane_id"),
                                                      event.get("workspace_id"))
    title, body = compose(event, ws_label, tab_label, pane_title)
    sent = emit(title, body)
    # Herdr already toasts background workspaces (with its own sparser text);
    # ours covers the workspace you are looking at, which Herdr deliberately
    # skips. Split that way, one event never produces two toasts.
    reason = toast(title, body) if focused else None
    trace("notified %d tty(s), toast=%s: %s | %s"
          % (sent, reason or "skipped", title, body))
    return 0


def self_check():
    assert osc777("T", "B") == "\033]777;notify;T;B\007"

    # Semicolons would split Warp's payload; newlines would truncate it.
    assert sanitize("a;b") == "a,b"
    assert sanitize("a\nb  c") == "a b c"
    assert sanitize("x" * 300, 10) == "x" * 9 + "…"
    assert sanitize(None) == ""

    # Only blocked/done notify.
    for status in ("working", "idle", "unknown", None):
        assert compose({"agent_status": status}, "proj") is None, status

    # Title = who wants what. Body = workspace › tab › what they are on.
    t, b = compose({"agent_status": "done", "display_agent": "Claude",
                    "title": "Fix login bug"}, "portal")
    assert t == "✅ Claude finished", t
    assert b == "portal › Fix login bug", b

    _, b = compose({"agent_status": "blocked", "agent": "claude",
                    "title": "add text to loading page"}, "portal", "WEB-1204")
    assert b == "portal › WEB-1204 › add text to loading page", b

    # A tab label that prefixes the pane title is redundant -- keep the longer.
    _, b = compose({"agent_status": "done", "agent": "c",
                    "title": "WEB-1204 - 27 Aug - main"}, "portal", "WEB-1204")
    assert b == "portal › WEB-1204 - 27 Aug - main", b

    # A bare agent id gets title-cased; a numeric tab label is dropped and the
    # pane title stands in when the event carries no title of its own. This is
    # the exact shape of the first real event observed: it produced
    # "claude · herdr-warp / Finished - 1" before any of this.
    t, b = compose({"agent_status": "done", "agent": "claude"},
                   "herdr-warp", "1", "Herdr notification plugin")
    assert t == "✅ Claude finished", t
    assert b == "herdr-warp › Herdr notification plugin", b

    # display_agent still wins when present, verbatim.
    t, _ = compose({"agent_status": "done", "agent": "claude",
                    "display_agent": "Claude Code"}, "w")
    assert t == "✅ Claude Code finished", t
    # Hyphenated ids read as words.
    t, _ = compose({"agent_status": "done", "agent": "antigravity-cli"}, "w")
    assert t == "✅ Antigravity Cli finished", t
    # No tab, no title -> the workspace alone still says which space.
    _, b = compose({"agent_status": "blocked", "agent": "c"}, "w", "3", "")
    assert b == "w", b

    # A tab label repeating the workspace adds nothing.
    _, b = compose({"agent_status": "done", "agent": "c"}, "portal", "portal")
    assert b == "portal", b

    # Herdr's own state wording wins over our fallback.
    t, b = compose({"agent_status": "blocked", "agent": "codex",
                    "state_labels": {"blocked": "needs input"}}, "api")
    assert (t, b) == ("🔔 Codex needs input", "api"), (t, b)

    # Nothing located at all -> say how to get back rather than nothing.
    t, b = compose({"agent_status": "done", "agent": "droid"}, "")
    assert (t, b) == ("✅ Droid finished", "Open Herdr to pick it up"), (t, b)

    # Real envelope shape, captured from the socket API.
    ev = unwrap('{"event":"pane_agent_status_changed","data":'
                '{"agent_status":"done","agent":"claude","pane_id":"w7:p5",'
                '"workspace_id":"w7","title":"T"}}')
    assert ev["agent_status"] == "done", ev
    assert compose(ev, "w") == ("✅ Claude finished", "w › T")
    assert compose(ev, "w", "tab1")[1] == "w › tab1 › T"

    # The in-app toast only fires under `delivery = "herdr"`, so the scan that
    # decides that has to read a real config shape.
    import tempfile
    home = os.environ.get("XDG_CONFIG_HOME")
    try:
        for body, want in (('[ui.toast]\ndelivery = "herdr"   # in-app\n', "herdr"),
                           ('[ui.toast]\ndelivery="terminal"\n', "terminal"),
                           ('[ui.toast.herdr]\nposition = "top-right"\n', "off"),
                           ('[ui]\naccent = "#d97757"\n', "off")):
            d = tempfile.mkdtemp()
            os.mkdir(os.path.join(d, "herdr"))
            with open(os.path.join(d, "herdr", "config.toml"), "w") as fh:
                fh.write(body)
            os.environ["XDG_CONFIG_HOME"] = d
            assert toast_delivery() == want, (body, toast_delivery())
        os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp()   # no config at all
        assert toast_delivery() == "off"
        # HERDR_CONFIG_PATH wins over the XDG guess.
        direct = os.path.join(tempfile.mkdtemp(), "c.toml")
        with open(direct, "w") as fh:
            fh.write('[ui.toast]\ndelivery = "system"\n')
        os.environ["HERDR_CONFIG_PATH"] = direct
        assert toast_delivery() == "system", toast_delivery()

        # The dwell hold, against a fake server. Re-show the moment the slot
        # frees, and give up rather than spin if it never does.
        with open(direct, "w") as fh:
            fh.write('[ui.toast]\ndelivery = "herdr"\n')
        real_call, real_sleep = _call, time.sleep
        try:
            time.sleep = lambda _s: None
            calls = []

            def fake(reasons):
                def call(method, params):
                    calls.append(method)
                    r = reasons[min(len(calls), len(reasons)) - 1]
                    return {"result": {"reason": r}}
                return call

            # shown, then busy twice, then the re-show lands.
            globals()["_call"] = fake(["shown", "busy", "busy", "shown"])
            assert toast("t", "b") == "shown"
            assert len(calls) == 4, calls

            # Never frees -> bounded by TOAST_POLL_LIMIT, not an infinite loop.
            del calls[:]
            globals()["_call"] = fake(["shown", "busy"])
            assert toast("t", "b") == "shown"
            assert len(calls) <= 2 + TOAST_POLL_LIMIT / TOAST_POLL_SECONDS, calls

            # A refused toast is not held.
            del calls[:]
            globals()["_call"] = fake(["disabled"])
            assert toast("t", "b") == "disabled"
            assert len(calls) == 1, calls
        finally:
            globals()["_call"] = real_call
            time.sleep = real_sleep
    finally:
        os.environ.pop("HERDR_CONFIG_PATH", None)
        os.environ.pop("XDG_CONFIG_HOME", None)
        if home:
            os.environ["XDG_CONFIG_HOME"] = home
    # A bare payload still works.
    assert unwrap('{"agent_status":"idle"}')["agent_status"] == "idle"
    assert unwrap("not json") is None
    assert unwrap("[1,2]") is None

    # `herdr server` and user-typed subcommands are not clients.
    assert _is_client_argv(["/usr/bin/herdr"], "herdr")
    assert _is_client_argv(["herdr", "--session", "work"], "herdr")
    assert not _is_client_argv(["herdr", "server"], "herdr")
    assert _is_client_argv(["herdr", "session", "attach", "work"], "herdr")
    assert not _is_client_argv(["herdr", "status"], "herdr")
    assert not _is_client_argv(["herdr", "pane", "list"], "herdr")
    assert not _is_client_argv(["python3", "notify.py"], "herdr")
    assert not _is_client_argv([], "herdr")
    print("self-check ok")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--self-check":
        self_check()
    elif arg == "--test":
        title, body = "🔔 Claude needs you", "herdr-warp › main › self-test"
        n = emit(title, body)
        print("osc 777 -> %d client tty(s): %s" % (n, client_ttys() or "none found"))
        print("herdr toast (delivery=%s): %s"
              % (toast_delivery(), toast(title, body) or "skipped"))
    else:
        sys.exit(main())
