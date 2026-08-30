# herdr-warp

A Herdr plugin that turns `pane.agent_status_changed` into a Warp desktop
notification and a Herdr in-app toast. Two files do the work:

| File | Role |
|---|---|
| `herdr-plugin.toml` | the manifest: id, version, and the one event hook |
| `notify.py` | the hook. Stdlib only, targets **system python 3.9** (macOS) |

`README.md` is the manual. Anything you change about *what the user sees or
configures* is not finished until the README says the same thing — the
notification text, the `[ui.toast]` requirements, the dwell knobs, the corner
cases table. It is documentation, not decoration.

## Commits

**Conventional Commits.** `semantic-release` publishes from `main`, so the
prefix is not cosmetic — it decides whether a release happens and what number
it gets:

| Prefix | Release |
|---|---|
| `feat:` | minor |
| `fix:` | patch |
| `perf:` | patch |
| `docs:` `chore:` `refactor:` `test:` `style:` `ci:` `build:` | none |
| `BREAKING CHANGE:` in the body | major |

README-only work is `docs:`. A `feat:` that ships no user-visible behaviour
cuts a version nobody needed; a behaviour change committed as `chore:` ships
to installs that never learn about it. Pick the prefix for what the user gets,
not for how much code moved.

Write the body for someone reading `git log` a year from now: what was wrong,
what it does instead, and the constraint that ruled out the obvious fix. The
subject line is the changelog entry — it is published verbatim to GitHub
Releases, so make it read as a sentence about the product.

## Releases

Never bump `version` in `herdr-plugin.toml` by hand. `semantic-release` owns
it: `.releaserc.json` seds the manifest, writes `CHANGELOG.md`, commits both as
`chore(release): x.y.z [skip ci]`, tags `vx.y.z`, and cuts a GitHub Release.
A hand-edited version drifts from the tag, and Herdr resolves
`herdr plugin install --ref vx.y.z` against tags.

Dry run before you push anything release-shaped:

```bash
npm ci && npm run release:dry
```

## Rules

- **`notify.py` is stdlib-only and 3.9-compatible.** Herdr runs it with
  whatever `python3` is on PATH; on macOS that is 3.9, which has no `tomllib`,
  no `match`, and no `X | Y` annotations. No pip, ever — a hook that needs an
  install is a hook that fails silently on someone else's machine.
- **Every change to `notify.py` lands with its assertion** in `self_check()`.
  That function is the entire test suite and CI runs it; `python3 notify.py
  --self-check` must pass before you commit.
- **An event hook has nowhere to print.** `trace()` is the only way to see what
  a real run did — `touch $HERDR_PLUGIN_CONFIG_DIR/debug` arms it.
- **Never write OSC to our own process ancestry.** Inside a pane that resolves
  to the pane's PTY, and Herdr swallows OSC 777 arriving from panes. Locate the
  herdr *client* process instead; no client attached means no notification,
  which is the correct answer and not a failure.
- Herdr-side facts (dwell time, `busy`, which delivery draws in-app) were
  measured against a running server, not read off the docs. If you change a
  number, measure it again and say so in the commit.
