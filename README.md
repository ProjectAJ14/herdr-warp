# herdr-warp

Native [**Warp**](https://warp.dev) desktop notifications when a
[**Herdr**](https://herdr.dev)-managed coding agent finishes or needs your
attention.

Modelled on [`warpdotdev/claude-code-warp`](https://github.com/warpdotdev/claude-code-warp),
but this is a **Herdr plugin, not a Claude Code plugin** — it hooks Herdr's own
agent-state machine, so it covers *every* agent Herdr manages rather than one.

|                  | `claude-code-warp`                     | `herdr-warp`                          |
|------------------|----------------------------------------|---------------------------------------|
| Installs into    | Claude Code                            | **Herdr**                             |
| Manifest         | `.claude-plugin/plugin.json` + `hooks/`| `herdr-plugin.toml`                   |
| Triggered by     | Claude Code hooks (`Stop`, …)          | Herdr event `pane.agent_status_changed`|
| Covers           | Claude Code only                       | **every agent Herdr integrates**      |
| Notification     | OSC 777 `warp://cli-agent` (structured)| OSC 777 `notify` (plain) — see below  |

It touches nothing in `~/.claude/`. It also *replaces* `claude-code-warp` when you
run Claude Code inside Herdr: Herdr parses OSC 777 arriving from panes, so that
plugin's notifications are swallowed and re-emitted in a format Warp ignores.

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
                    ┌──────────────────────── Warp (macOS app) ──────────────────────┐
                    │  /dev/ttysNNN                                                  │
                    │      ▲                                                         │
                    │      │  ESC]777;notify;<title>;<body> BEL   ← notify.py writes │
                    │      │                                         here, directly  │
                    │  herdr client (TUI, owns this tty)                             │
                    └──────┬─────────────────────────────────────────────────────────┘
                           │ unix socket
                    ┌──────┴─────────── herdr server (headless) ─────────────────────┐
                    │                                                                │
                    │  pane PTYs ──► agent-state detection                           │
                    │                      │                                         │
                    │                      ├─► sound            (herdr, works today) │
                    │                      ├─► OSC 9 / OSC 99   (Warp ignores these) │
                    │                      └─► pane.agent_status_changed              │
                    │                                  │                             │
                    │                                  ▼                             │
                    │                             notify.py  ── plugin event hook     │
                    └────────────────────────────────────────────────────────────────┘
```

The one thing to notice: **Warp sees a single PTY for the whole Herdr session**,
no matter how many panes and agents run behind it. That single fact drives two
design choices — a plain `notify` rather than the structured protocol, and the
click-targeting limit. Both are explained under [Corner cases](#corner-cases).

`notify.py` locates the herdr client process and writes to its tty. It does *not*
fall back to its own process ancestry: inside a pane that resolves to the pane's
own PTY, and Herdr swallows OSC 777 arriving from panes. No client attached means
no outer terminal to notify, which is the correct answer rather than a failure.

It notifies on two of Herdr's five agent states, matching Herdr's own two sound
categories (`request_path` = needs-attention, `done_path` = finished):

| Herdr state | Notifies | Meaning                       |
|-------------|----------|-------------------------------|
| `blocked`   | **yes**  | needs your input / permission |
| `done`      | **yes**  | task finished                 |
| `working`   | no       | in progress                   |
| `idle`      | no       | sitting at its prompt         |
| `unknown`   | no       | not an agent pane             |

Notification text is `agent · workspace` over `state — where`:

```
Claude · portal
Needs your attention — WEB-1204
```

Herdr's own wording for the state is used when the event carries it. Real events
are sparser than the schema allows, so: a missing `display_agent` falls back to a
title-cased agent id, a missing `title` falls back to the pane's
`terminal_title_stripped`, and Herdr's default numeric tab labels (`1`, `2`, …)
are dropped rather than printed as `Finished — 1`.

## Install

```bash
herdr plugin install ProjectAJ14/herdr-warp
herdr plugin list        # herdr-warp enabled, and NO "unknown event" warning
```

Then **restart Herdr** — a plugin linked or installed mid-session is registered
but its event hooks are not wired until the next server start. See
[Activation](#activation).

Requires Herdr ≥ 0.8.0, Warp, and `python3` (Herdr's own Claude integration
already depends on python3). No `jq`, no other dependencies.

For local development instead:

```bash
git clone https://github.com/ProjectAJ14/herdr-warp
herdr plugin link ./herdr-warp
```

## Verify

```bash
cd "$(herdr plugin list --json 2>/dev/null | \
      python3 -c 'import sys,json;print(json.load(sys.stdin)["result"]["plugins"][0]["plugin_root"])')"
python3 notify.py --self-check   # offline assertions
python3 notify.py --test         # fire one real notification
```

For `--test`, **switch away from Warp** — Warp raises a desktop notification only
while it is *not* the focused app, so a test run with Warp in front looks like a
silent failure.

`--test` also prints how many client ttys it wrote to. `0` means no Herdr client
is attached, so there is no outer terminal to notify.

## Uninstall

```bash
herdr plugin uninstall herdr-warp   # installed from GitHub
herdr plugin unlink herdr-warp      # linked locally
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
CFG="$(herdr plugin config-dir herdr-warp)"
touch "$CFG/debug" "${CFG/\/config\///state/}/debug"
# ... let an agent finish or block ...
cat "$CFG/debug.log" "${CFG/\/config\///state/}/debug.log" 2>/dev/null
```

Herdr passes both `HERDR_PLUGIN_CONFIG_DIR` and `HERDR_PLUGIN_STATE_DIR`, and
which one a hook run sees is not guaranteed, so flag both. The log lands next to
whichever flag is found.

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

## References

- [Herdr — plugins](https://herdr.dev/docs/plugins) — manifest format, event hooks, plugin env vars
- [Herdr — configuration](https://herdr.dev/docs/configuration) — `[ui.toast]`, `[ui.sound]`, keybindings
- [Herdr — socket API](https://herdr.dev/docs/socket-api) — `pane.agent_status_changed`, `session.snapshot`
- [Warp — desktop notifications](https://docs.warp.dev/terminal/more-features/notifications/) — OSC 9 and OSC 777 formats
- [Warp issue #7896](https://github.com/warpdotdev/Warp/issues/7896) — OSC 9 support, shipped 2026-03
- [`warpdotdev/claude-code-warp`](https://github.com/warpdotdev/claude-code-warp) — the Claude Code equivalent this is modelled on

## License

MIT
