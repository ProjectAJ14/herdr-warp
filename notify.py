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

Clicking the desktop notification lands on the pane that raised it: a detached
waiter sees Warp come back to the front and calls `pane.focus` (macOS only).

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

# Click-to-jump. Warp's notification click only brings Warp forward -- it has
# no callback and cannot see herdr panes -- so we watch for the effect instead:
# once Warp is back in front, `pane.focus` the pane that notified. That call
# switches workspace, tab and pane in one go (measured). Set JUMP_WAIT_SECONDS
# = 0 to turn it off.
JUMP_WAIT_SECONDS = 1800
JUMP_POLL_SECONDS = 0.3
WARP_BUNDLE_PREFIX = "dev.warp."

# `herdr session attach <name>` is the only client form that leads with a word.
CLIENT_SUBCOMMANDS = ("session",)


# A pane title is whatever the program inside the pane last wrote, so it is
# untrusted: an ESC or BEL in it would close our OSC sequence early and leave
# the rest on the tty as literal text. Whitespace controls are left in for
# split() below to fold into spaces; the rest are dropped.
CONTROL = dict.fromkeys(
    [c for c in range(0x20) if chr(c) not in " \t\n\r\v\f"] + [0x7F])


def sanitize(text, limit=200):
    """Warp's OSC 777 payload is semicolon-delimited, single-line, BEL-ended."""
    if not text:
        return ""
    out = " ".join(str(text).translate(CONTROL).split()).replace(";", ",")
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


def warp_in_front():
    """True/False on macOS; None where there is no `lsappinfo` to ask."""
    try:
        asn = subprocess.run(["lsappinfo", "front"], capture_output=True,
                             text=True, timeout=3).stdout.strip()
        info = subprocess.run(["lsappinfo", "info", "-only", "bundleid", asn],
                              capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return ('"%s' % WARP_BUNDLE_PREFIX) in info


def _state_dir():
    return (os.environ.get("HERDR_PLUGIN_STATE_DIR")
            or os.environ.get("HERDR_PLUGIN_CONFIG_DIR"))


def arm_jump(pane_id):
    """Land on `pane_id` the next time Warp comes to the front.

    Only armed while Warp is in the background -- that is when Warp shows the
    desktop notification, so coming back to Warp means you clicked it (or
    switched back yourself, which is just as good a reason to land there).

    The target lives in a file and one detached waiter polls for it, so a burst
    of events costs one poller and the latest one wins. The file carries the
    socket too: the state dir is shared by every herdr session, and the waiter
    may have been started by a different one.
    """
    d = _state_dir()
    sock = os.environ.get("HERDR_SOCKET_PATH")
    if not (JUMP_WAIT_SECONDS and pane_id and d and sock) \
            or warp_in_front() is not False:
        return False
    try:
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "jump"), "w") as fh:
            fh.write("%s\n%s\n" % (sock, pane_id))
        # Detach: the hook returns now; the waiter outlives it in its own
        # session, with no pipes back to Herdr to hold open.
        if os.fork():
            return True
    except OSError:
        return False
    try:
        os.setsid()
        null = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(null, fd)
        _wait_and_jump(d)
    finally:
        os._exit(0)


def _wait_and_jump(d):
    import fcntl
    lock = open(os.path.join(d, "jump.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return      # a waiter is already running; it reads our target
    target = os.path.join(d, "jump")
    # The clock is the target's mtime, so each new event restarts it and the
    # latest notification always gets the full window.
    while True:
        try:
            age = time.time() - os.path.getmtime(target)
        except OSError:
            return          # already taken
        if age >= JUMP_WAIT_SECONDS:
            # Expired: coming back hours later should not yank you anywhere.
            # ponytail: an event written between the stat and this remove is
            # dropped -- a window of one poll, once per JUMP_WAIT_SECONDS.
            try:
                os.remove(target)
            except OSError:
                pass
            return
        if warp_in_front():
            break
        time.sleep(JUMP_POLL_SECONDS)
    try:
        with open(target) as fh:
            sock, pane_id = fh.read().splitlines()[:2]
        os.remove(target)
    except (OSError, ValueError):
        return
    os.environ["HERDR_SOCKET_PATH"] = sock
    reply = _call("pane.focus", {"pane_id": pane_id})
    trace("jumped to %s: %s"
          % (pane_id, "ok" if reply and "result" in reply else reply))


def locate(pane_id, workspace_id):
    """(workspace label, tab label, pane title, workspace is focused).

    Warp sees one PTY for every herdr pane, so its notification click only
    reaches the herdr tab; arm_jump() does the rest for the latest one. Naming
    the workspace and tab in the body covers the others, as does Herdr's own
    `prefix+o` (open_notification_target).

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
    armed = sent and arm_jump(event.get("pane_id"))
    # Herdr already toasts background workspaces (with its own sparser text);
    # ours covers the workspace you are looking at, which Herdr deliberately
    # skips. Split that way, one event never produces two toasts.
    reason = toast(title, body) if focused else None
    trace("notified %d tty(s), toast=%s, jump=%s: %s | %s"
          % (sent, reason or "skipped", "armed" if armed else "no",
             title, body))
    return 0


def check_jump():
    """Click-to-jump: the frontmost probe, the waiter, the guards, and the real
    fork against a fake herdr socket. Part of --self-check."""
    import fcntl
    import tempfile
    import threading

    saved = (_call, time.sleep, warp_in_front, subprocess.run,
             dict(os.environ))
    d = tempfile.mkdtemp()
    target = os.path.join(d, "jump")

    def put(pane_id, sock="/tmp/herdr-b.sock", age=0):
        with open(target, "w") as fh:
            fh.write("%s\n%s\n" % (sock, pane_id))
        if age:
            t = time.time() - age
            os.utime(target, (t, t))

    try:
        # warp_in_front parses `lsappinfo info`, and says None where there is
        # no lsappinfo (Linux) so the caller never arms.
        def run_with(bundle):
            def run(argv, **_kw):
                if bundle is None:
                    raise OSError("no lsappinfo")
                out = ("ASN:0x0-0x1:" if argv[1] == "front" else
                       '[ NULL ]  ASN:0x0-0x1: (in front)\n'
                       '    bundleID="%s"\n' % bundle)
                return subprocess.CompletedProcess(argv, 0, out, "")
            return run
        for bundle, want in (("dev.warp.Warp-Stable", True),
                             ("dev.warp.Warp-Preview", True),
                             ("com.apple.finder", False),
                             ("com.example.dev.warp.fake", False),
                             (None, None)):
            subprocess.run = run_with(bundle)
            assert warp_in_front() is want, (bundle, warp_in_front())
        subprocess.run = saved[3]

        time.sleep = lambda _s: None
        calls = []
        globals()["_call"] = lambda m, p: calls.append(
            (m, p, os.environ.get("HERDR_SOCKET_PATH"))) or {"result": {}}

        # Away, away, back -> focus it once, over the socket the *file* names
        # (a waiter started by another herdr session must not use its own).
        os.environ["HERDR_SOCKET_PATH"] = "/tmp/herdr-a.sock"
        fronts = iter([False, False, True])
        globals()["warp_in_front"] = lambda: next(fronts)
        put("w7:p5")
        _wait_and_jump(d)
        assert calls == [("pane.focus", {"pane_id": "w7:p5"},
                          "/tmp/herdr-b.sock")], calls
        assert not os.path.exists(target)

        # A socket path with a space survives the round trip.
        del calls[:]
        globals()["warp_in_front"] = lambda: True
        put("w1:p1", "/tmp/my herdr/h.sock")
        _wait_and_jump(d)
        assert calls[0][2] == "/tmp/my herdr/h.sock", calls

        # Expired -> drop the target without jumping, even if Warp is in front.
        del calls[:]
        put("w7:p5", age=JUMP_WAIT_SECONDS + 1)
        _wait_and_jump(d)
        assert calls == [] and not os.path.exists(target), calls

        # The window is the target's own age, not the waiter's: a waiter
        # started long ago still jumps to a target written a minute before
        # the window closes.
        fronts = iter([False, True])
        globals()["warp_in_front"] = lambda: next(fronts)
        put("w2:p2", age=JUMP_WAIT_SECONDS - 60)
        _wait_and_jump(d)
        assert [c[1] for c in calls] == [{"pane_id": "w2:p2"}], calls

        # Never back -> the loop ends by the clock, not by running forever.
        del calls[:]
        now = [time.time()]
        real_time = time.time
        time.time = lambda: now[0]
        globals()["warp_in_front"] = lambda: False
        time.sleep = lambda s: now.__setitem__(0, now[0] + s)
        put("w7:p5")
        os.utime(target, (now[0], now[0]))
        _wait_and_jump(d)
        time.time = real_time
        time.sleep = lambda _s: None
        assert calls == [] and not os.path.exists(target), calls

        # A malformed target is dropped, not sent.
        globals()["warp_in_front"] = lambda: True
        with open(target, "w") as fh:
            fh.write("only-one-line")
        _wait_and_jump(d)
        assert calls == [], calls

        # A waiter already holds the lock -> leave the target to it.
        put("w7:p5")
        held = open(os.path.join(d, "jump.lock"), "w")
        fcntl.flock(held, fcntl.LOCK_EX)
        _wait_and_jump(d)
        held.close()
        assert calls == [] and os.path.exists(target), calls
        os.remove(target)

        # Guards: nothing is written and nothing forks unless Warp is known to
        # be in the background and there is somewhere to jump.
        os.environ["HERDR_PLUGIN_STATE_DIR"] = d
        for front, pane, sock in ((True, "w7:p5", "/s"),     # Warp in front
                                  (None, "w7:p5", "/s"),     # no lsappinfo
                                  (False, None, "/s"),       # no pane id
                                  (False, "w7:p5", None)):   # no socket
            globals()["warp_in_front"] = lambda f=front: f
            if sock:
                os.environ["HERDR_SOCKET_PATH"] = sock
            else:
                os.environ.pop("HERDR_SOCKET_PATH", None)
            assert arm_jump(pane) is False, (front, pane, sock)
            assert not os.path.exists(target), (front, pane, sock)

        # main() arms only when the desktop notification actually went out,
        # and with the event's own pane.
        armed = []
        real = (emit, locate, toast, arm_jump)
        g = globals()
        g["locate"] = lambda p, w: ("ws", "", "", False)
        g["toast"] = lambda t, b: None
        g["arm_jump"] = lambda p: armed.append(p) or True
        os.environ["HERDR_PLUGIN_EVENT_JSON"] = json.dumps(
            {"agent_status": "done", "agent": "claude", "pane_id": "w3:p4",
             "workspace_id": "w3"})
        for sent, want in ((1, ["w3:p4"]), (0, [])):
            del armed[:]
            g["emit"] = lambda t, b, n=sent: n
            main()
            assert armed == want, (sent, armed)
        os.environ["HERDR_PLUGIN_EVENT_JSON"] = '{"agent_status":"working"}'
        del armed[:]
        g["emit"] = lambda t, b: 1
        main()
        assert armed == [], armed
        g["emit"], g["locate"], g["toast"], g["arm_jump"] = real

        # The real thing: fork a detached waiter and watch it call a fake herdr
        # server. Warp is "away" when armed and "back" on the waiter's first
        # look -- the child inherits this counter, so both sides agree.
        globals()["_call"] = saved[0]
        time.sleep = saved[1]
        sock = os.path.join(tempfile.mkdtemp(), "h.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(sock)
        server.listen(1)
        server.settimeout(5)
        got = []

        def serve():
            try:
                conn, _ = server.accept()
                got.append(json.loads(conn.makefile().readline()))
                conn.sendall(b'{"id":"x","result":{}}\n')
                conn.close()
            except OSError:
                pass
        th = threading.Thread(target=serve)
        th.start()
        looks = [0]

        def front():
            looks[0] += 1
            return looks[0] > 1
        globals()["warp_in_front"] = front
        os.environ["HERDR_SOCKET_PATH"] = sock
        started = time.time()
        assert arm_jump("w9:p1") is True
        assert time.time() - started < 1, "the hook must not wait for the click"
        th.join(10)
        server.close()
        assert got and got[0]["method"] == "pane.focus", got
        assert got[0]["params"] == {"pane_id": "w9:p1"}, got
        for _ in range(50):             # the waiter clears it after the call
            if not os.path.exists(target):
                break
            time.sleep(0.05)
        assert not os.path.exists(target)
    finally:
        (globals()["_call"], time.sleep, globals()["warp_in_front"],
         subprocess.run, env) = saved
        os.environ.clear()
        os.environ.update(env)


def self_check():
    assert osc777("T", "B") == "\033]777;notify;T;B\007"

    # Semicolons would split Warp's payload; newlines would truncate it.
    assert sanitize("a;b") == "a,b"
    assert sanitize("a\nb  c") == "a b c"
    assert sanitize("x" * 300, 10) == "x" * 9 + "…"
    assert sanitize(None) == ""
    # A pane title carrying ESC/BEL must not be able to close the sequence.
    assert sanitize("a\x07b") == "ab"
    assert sanitize("\033]777;notify;x") == "]777,notify,x"
    assert "\033" not in osc777(sanitize("\033x"), "b")[1:]

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
    check_jump()

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
