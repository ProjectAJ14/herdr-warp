# herdr-warp

Native **Warp** desktop notifications when a **Herdr**-managed coding agent finishes
or needs your attention.

Modelled on [`warpdotdev/claude-code-warp`](https://github.com/warpdotdev/claude-code-warp),
but hooked into Herdr instead of Claude Code — so it covers *every* agent Herdr
manages (Claude, Codex, Copilot, Droid, Kimi, Cursor, …), not just one.

## Why this is needed

Herdr already detects agent state and plays a sound. It does not get a desktop
notification out of Warp, for two independent reasons found by inspecting the
`herdr` binary and Warp's protocol:

**1. Escape-sequence mismatch.** Herdr's `[ui.toast] delivery = "terminal"` emits,
from `src/ui/panes.rs`:

```
ESC ] 9 ; <body> ESC \                    # OSC 9,  iTerm2 style
ESC ] 99 ; i=1:d=0 ; <title> ESC \        # OSC 99, kitty style
ESC ] 99 ; i=1:p=body ; <body> ESC \
```

Note the **ST terminator** (`ESC \`). Warp's notification parser documents **BEL**
(`\007`), and Warp's own agent integration uses **OSC 777**
(`ESC ] 777 ; notify ; <title> ; <body> BEL`). The string `777` never appears in
the Herdr binary — the two never meet.

**2. Focus scope.** Herdr only toasts *background* workspaces ("Background
notification popup delivery"). The workspace you are looking at never notifies —
even when you have tabbed away from Warp entirely, which is precisely when you
want to be told. Herdr cannot know that Warp lost OS focus.

Warp, by contrast, raises a desktop notification only while Warp itself is
unfocused. That is exactly the right gate — so this plugin notifies on every
attention-worthy transition and lets Warp decide.

> Bonus finding: Herdr *parses* OSC 777 coming from panes (its rxvt-notify
> extension) and converts it into its own toast. So the `claude-code-warp` plugin
> is silently swallowed when Claude Code runs inside a Herdr pane. This plugin is
> the replacement for that path.

## How it works

One event hook, one script.

```
pane.agent_status_changed  ──►  notify.py  ──►  ESC]777;notify;title;body BEL  ──►  Warp
```

`notify.py` writes straight to the tty the **herdr client** is attached to (Warp's
PTY), bypassing both the format mismatch and the background-only filter. It finds
that tty by locating the herdr client process, falling back to walking its own
process ancestry.

It notifies on two of Herdr's five agent states, matching Herdr's own two sound
categories:

| Herdr state | Notifies | Meaning                          |
|-------------|----------|----------------------------------|
| `blocked`   | yes      | needs your input / permission    |
| `done`      | yes      | task finished                    |
| `working`   | no       | —                                |
| `idle`      | no       | —                                |
| `unknown`   | no       | —                                |

Notification text uses Herdr's own wording where it provides it:

```
Claude · order-integrity-platform
Needs your attention — Message explanation with examples
```

## Install

```bash
herdr plugin link /path/to/herdr-warp
herdr plugin list                       # should show herdr-warp enabled
```

Requires Herdr ≥ 0.8.0, Warp, and `python3` (Herdr's own Claude integration
already depends on python3). No `jq`, no other dependencies.

## Verify

```bash
python3 notify.py --self-check   # offline assertions
python3 notify.py --test         # fire one real notification
```

For `--test`, **switch away from Warp** — Warp suppresses notifications while it
is the focused app.

Once linked, check the hook is firing after an agent changes state:

```bash
herdr plugin log list
```

## Uninstall

```bash
herdr plugin unlink herdr-warp
```

## Corner cases

### When Warp is visible, don't send a system notification

Covered, natively. Warp raises a desktop notification **only while Warp is not
the focused app** — so no system notification ever fires while you are looking
at it. That is why this plugin does not try to detect focus itself: Warp is
already the right gate, and it is the only component that knows.

### When Warp is visible, show it on the current screen instead

**Needs one config line from you.** Herdr's in-app toast is its own feature, and
`[ui.toast] delivery` picks exactly one delivery:

```toml
[ui.toast]
delivery = "herdr"      # in-app toast  <- recommended with this plugin
# delivery = "terminal" # OSC 9/99 to the outer terminal (Warp ignores these)
```

With `delivery = "terminal"` you get **neither**: Warp cannot read Herdr's OSC 9,
and Herdr suppresses its own in-app toast because it thinks the terminal is
handling it. Switch it to `"herdr"` and the two halves stop overlapping:

| You are looking at | What you get | From |
|---|---|---|
| the Herdr UI in Warp | in-app toast, bottom-right | Herdr (`delivery = "herdr"`) |
| another app | Warp desktop notification | this plugin (OSC 777) |

They are mutually exclusive by focus, so you never get both for one event.

Note that Herdr's in-app toast only fires for **background** workspaces — the
pane you are already watching does not toast itself, which is the intended
behaviour.

### Clicking the notification should land me on the right pane

**Partly. Two keystrokes, not one — and this is a hard limit, not an oversight.**

Warp sees a single PTY for the herdr client, no matter how many panes and agents
Herdr runs behind it. So a notification click can only reach *the Warp tab
running Herdr*; Warp has no concept of the herdr pane inside it. This is why the
plugin sends a plain `notify` rather than Warp's structured `warp://cli-agent`
protocol — the structured protocol keys on a session id, and with N agents
sharing one Warp pane it would only make the tab status flap, without buying any
click targeting.

What does work:

1. Click the notification → Warp comes forward on the Herdr tab.
2. Press **`prefix`+`o`** (`ctrl+b o` with your prefix) → Herdr's
   `open_notification_target` jumps to the pane that raised the notification.

To make step 2 optional, the notification names its own destination:

```
Claude · portal
Needs your attention — WEB-1204 - 27 Aug 26 - feat/WEB-1204/...
```

Title is `agent · workspace`; the body leads with the tab label, so one glance
tells you which workspace and tab to switch to. The tab label is dropped when the
pane title already starts with it, and when it just repeats the workspace name.

## Notes

- Keep Herdr's sound on (`[ui.sound] enabled = true`) if you want both; this
  plugin only adds the visual notification.
- `[ui.toast] delivery` can stay as-is. Set it to `"off"` if you want to avoid
  Herdr's in-app toast on top of the Warp notification.
- Which states notify is a two-entry dict at the top of `notify.py`
  (`NOTIFY_STATES`) — edit it if you want `idle` too.

## Troubleshooting

Event hooks are fire-and-forget with nowhere to print, so `notify.py` has an
opt-in breadcrumb log:

```bash
touch "$(herdr plugin config-dir herdr-warp)/debug"
# ... let an agent finish or block ...
cat "$(herdr plugin config-dir herdr-warp)/debug.log"
```

Each line records the raw event Herdr passed in and how many ttys were written
to. Delete the `debug` file to turn it back off.

If it never fires, confirm the hook is registered:

```bash
herdr plugin list
```

## Activation

`herdr plugin link` registers the plugin immediately (`herdr plugin list` shows
it), but the running Herdr server **does not wire event hooks for a plugin
linked mid-session**. Verified on 0.8.2-preview: with a valid event name and no
link warnings, the event fires on the socket API while the hook command is never
spawned, and `herdr server reload-config` does not change it. The docs say the
same for startup hooks — they run on server start and on live handoff, "but not
when a client attaches, config reloads, or a plugin is linked or enabled."

So after linking, the plugin activates on the next server start. Either restart
Herdr, or trigger a live handoff, which replaces the server while keeping the
session and its panes alive:

```bash
herdr update --handoff
```

> Caveat, measured: driving the handoff through the raw `server.live_handoff` API
> method swaps the server and preserves every pane, but the **client exits and
> does not reattach** — you have to run `herdr` again to get the UI back. Prefer
> `herdr update --handoff`, where the client opts in and follows.

## Valid event names

Herdr's plugin event vocabulary is *not* the same as its socket-API
subscription list, and an unknown name is only reported as a non-fatal warning
on `herdr plugin list` — the hook silently never fires. Of the 26 socket event
names, 22 are valid plugin events; these four are **rejected**:

```
layout.updated   pane.output_changed   pane.updated   workspace.metadata_updated
```

After linking, always check:

```bash
herdr plugin list        # must show no "unknown event" warning
```
