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
# (`request_path` = needs-attention, `done_path` = finished).
NOTIFY_STATES = {
    "blocked": "Needs your attention",
    "done": "Finished",
}

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


def locate(pane_id, workspace_id):
    """(workspace label, tab label) so the notification says where to go.

    A Warp notification click can only reach the Warp tab hosting the herdr
    client -- Warp sees one PTY for every herdr pane -- so naming the workspace
    and tab is what actually gets the reader to the right place. Herdr's own
    `prefix+o` (open_notification_target) jumps there directly.
    """
    reply = _call("session.snapshot", {})
    try:
        snap = reply["result"]["snapshot"]
    except (TypeError, KeyError):
        return workspace_id or "", ""

    ws_label, tab_id = workspace_id or "", None
    for ws in snap.get("workspaces", []):
        if ws.get("workspace_id") == workspace_id:
            ws_label = ws.get("label") or ws_label
            break
    for pane in snap.get("panes", []) + snap.get("agents", []):
        if pane.get("pane_id") == pane_id:
            tab_id = pane.get("tab_id")
            break
    tab_label = ""
    if tab_id:
        for tab in snap.get("tabs", []):
            if tab.get("tab_id") == tab_id:
                tab_label = tab.get("label") or ""
                break
    return ws_label, tab_label


def compose(event, ws_label, tab_label=""):
    """(title, body) for an event, or None if this event is not worth a toast."""
    status = event.get("agent_status")
    if status not in NOTIFY_STATES:
        return None
    agent = event.get("display_agent") or event.get("agent") or "Agent"
    # Herdr's own wording for the state when it has one, else our fallback.
    state = (event.get("state_labels") or {}).get(status) or NOTIFY_STATES[status]

    title = sanitize("%s · %s" % (agent, ws_label) if ws_label else agent, 80)
    body = sanitize(state.capitalize(), 80)

    # Name the destination: tab first, then the pane/conversation title. Skip
    # either when it just repeats what is already on screen.
    detail = []
    for part in (sanitize(tab_label, 40), sanitize(event.get("title"), 120)):
        if part and part not in detail and part != body and part != ws_label:
            detail.append(part)
    # A tab label is often a prefix of the pane title; keep only the longer one.
    if len(detail) == 2 and detail[1].startswith(detail[0]):
        detail = [detail[1]]
    if detail:
        body = "%s — %s" % (body, " · ".join(detail))
    return title, body


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
    state = (os.environ.get("HERDR_PLUGIN_STATE_DIR")
             or os.environ.get("HERDR_PLUGIN_CONFIG_DIR"))
    if not state or not os.path.exists(os.path.join(state, "debug")):
        return
    try:
        with open(os.path.join(state, "debug.log"), "a") as fh:
            fh.write("%s %s\n" % (int(time.time()), msg))
    except OSError:
        pass


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
    ws_label, tab_label = locate(event.get("pane_id"), event.get("workspace_id"))
    title, body = compose(event, ws_label, tab_label)
    sent = emit(title, body)
    trace("notified %d tty(s): %s | %s" % (sent, title, body))
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

    t, b = compose({"agent_status": "done", "display_agent": "Claude",
                    "title": "Fix login bug"}, "portal")
    assert t == "Claude · portal", t
    assert b == "Finished — Fix login bug", b

    # The tab label names the destination within the workspace.
    _, b = compose({"agent_status": "blocked", "agent": "claude",
                    "title": "add text to loading page"}, "portal", "WEB-1204")
    assert b == "Needs your attention — WEB-1204 · add text to loading page", b

    # A tab label that prefixes the pane title is redundant -- keep the longer.
    _, b = compose({"agent_status": "done", "agent": "c",
                    "title": "WEB-1204 - 27 Aug - main"}, "portal", "WEB-1204")
    assert b == "Finished — WEB-1204 - 27 Aug - main", b

    # A tab label repeating the workspace adds nothing.
    _, b = compose({"agent_status": "done", "agent": "c"}, "portal", "portal")
    assert b == "Finished", b

    # Herdr's own state wording wins over our fallback.
    t, b = compose({"agent_status": "blocked", "agent": "codex",
                    "state_labels": {"blocked": "needs input"}}, "api")
    assert (t, b) == ("codex · api", "Needs input"), (t, b)

    # A duplicated detail must not be repeated in the body.
    _, b = compose({"agent_status": "done", "agent": "c", "title": "Finished"},
                   "w")
    assert b == "Finished", b

    # No workspace label -> agent name alone, never a stray separator.
    t, _ = compose({"agent_status": "done", "agent": "droid"}, "")
    assert t == "droid", t

    # Real envelope shape, captured from the socket API.
    ev = unwrap('{"event":"pane_agent_status_changed","data":'
                '{"agent_status":"done","agent":"claude","pane_id":"w7:p5",'
                '"workspace_id":"w7","title":"T"}}')
    assert ev["agent_status"] == "done", ev
    assert compose(ev, "w") == ("claude · w", "Finished — T")
    assert compose(ev, "w", "tab1")[1] == "Finished — tab1 · T"
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
        n = emit("Herdr → Warp", "Test notification. Switch away from Warp to see these.")
        print("wrote to %d client tty(s): %s" % (n, client_ttys() or "none found"))
    else:
        sys.exit(main())
