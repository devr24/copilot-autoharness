<h1 align="center">Copilot AutoHarness</h1>
<p align="center"><strong>Self-Learning Skills for GitHub Copilot CLI</strong></p>

<p align="center">
  <a href="https://github.com/devr24/copilot-autoharness/actions/workflows/ci.yml"><img src="https://github.com/devr24/copilot-autoharness/actions/workflows/ci.yml/badge.svg" alt="CI" /></a> <img src="https://img.shields.io/badge/release-v0.1.0-brightgreen.svg" alt="release" /> <img src="https://img.shields.io/badge/python-3.11%2B-blue.svg" alt="python" /> <img src="https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey.svg" alt="platform" /> <img src="https://img.shields.io/badge/license-MIT-yellow.svg" alt="license MIT" />
</p>

**Copilot AutoHarness is a self-learning skill layer for GitHub Copilot CLI.** It **learns** skills from your
real sessions, **merges** near-duplicates instead of stacking them, **versions** every change, and keeps
**humans in control of risky knowledge** — all as native Copilot Agent Skills, **touching only the skills
it wrote itself**.

It is the Copilot analogue of [tigerless-labs/autoharness](https://github.com/tigerless-labs/autoharness),
rebuilt on Copilot CLI's documented hooks and native skills. Same idea — the harness around the model
matters, and one slice of it, the skill layer, can maintain itself — with a stronger emphasis on
provenance, trust states and rollback.

| | |
|---|---|
| **Learns from real work** | Each episode is distilled from the session you were already having — your prompts, Copilot's replies and tool activity. It fires on its own once a session has done enough work; `harness learn --now` distills on demand. |
| **Groups, doesn't just pile up** | The reflector sees an index of existing skills and can `patch` instead of `create`. A periodic curator folds near-duplicates under one skill; absorbed skills are archived with `merged_into` recorded, so a merge is never mistaken for a deletion. |
| **Keeps its own library in view** | Every session opens with an index of the skills it wrote (trusted first, capped), injected through the `sessionStart` hook. Copilot's own skill recall is left untouched. |
| **Humans gate risky knowledge** | Low-risk skills enter *probation*. Anything touching production, deployment, IAM, secrets, data migration or destructive steps is held until you `harness accept` it. |
| **Provenance, versions, rollback** | Every skill records its source session, reason, creator, validation result and Harness version. Previous bodies are versioned with SHA-256 digests and can be rolled back, quarantined or archived. |
| **Validated before trusted** | `harness shadow` replays task cases against a revision and its predecessor and reports regressions and likely conflicts, without touching the live skill. |
| **Shared through review** | Trusted skills carry an owner, criticality and expiry, export with minimised provenance, and move between teams via a CI-verified, code-owner-reviewed repository. Imports only ever create a pending proposal. |
| **Only its own skills** | Create never overwrites; patch, trust, quarantine, archive and rollback require Harness ownership metadata. Skills you wrote or installed are never touched. |
| **Local-first, no daemon** | State lives in a local SQLite file. Hooks start short-lived processes; there is no resident service and no telemetry. |

## Install

**Requires Python 3.11+ and GitHub Copilot CLI** (installed and authenticated). Harness uses only the
Python standard library. Copilot spawns hooks without a shell, so `harness` must resolve to a real
executable (`harness.exe` on Windows), not a `.cmd` shim.

```powershell
py -m pip install git+https://github.com/devr24/copilot-autoharness.git   # provides harness on PATH (python3 -m pip on Linux/macOS)
harness config init                  # explicit opt-in to automatic learning
copilot plugin marketplace add devr24/copilot-autoharness
copilot plugin install copilot-harness@copilot-harness-marketplace
harness doctor                       # verify the install
```

Start a fresh Copilot CLI session. Zero further config: Harness now watches your sessions and writes
learned skills into `.github/skills/` (project) or `~/.copilot/skills/` (personal) in the background.
Cadence and lifecycle thresholds are tunable — see [Configuration](#configuration).

Nothing to invoke, but one entry point exists when you want it: **`harness learn --now`** distills the
session you are in right now (from inside Copilot CLI: `!harness learn --now`).

> **Data handling:** reflection sends bounded, redacted conversation text, tool-event evidence and a sample
> of Harness-managed skills to the Copilot model provider. Pattern-based redaction is best-effort. Review
> your organisation's data policy before opting in. Without a config file, Harness stays in capture-only
> `off` mode.

Prefer not to use the plugin? Copy `hooks/hooks.json` into a repository's `.github/hooks/` — see the
[reference](docs/reference.md#enable-hooks-in-a-repository-manual-alternative). Don't use both; `harness doctor`
warns about double installation.

### Update

```powershell
py -m pip install --upgrade --force-reinstall git+https://github.com/devr24/copilot-autoharness.git
copilot plugin marketplace update copilot-harness-marketplace
copilot plugin update copilot-harness@copilot-harness-marketplace
```

Restart Copilot CLI to apply.

### Uninstall

```powershell
copilot plugin uninstall copilot-harness
copilot plugin marketplace remove copilot-harness-marketplace
py -m pip uninstall copilot-harness
```

Uninstalling only stops it running. The skills it landed and its state stay on disk. To clear those too,
delete the Harness home (`%LOCALAPPDATA%\copilot-harness\`, or `~/.local/state/copilot-harness/`) and the
Harness-owned skills under `.github/skills/` and `~/.copilot/skills/` (each carries a `.sidecar.json` and
`.ledger.jsonl`, so they are easy to tell from yours). Your own skills are never touched.

## Configuration

Settings live in `config.toml` in the Harness home (`harness config` prints the path). Defaults are
deliberate placeholders pending more live calibration.

**Cadence — when it learns**

| Setting | Default | What it does |
|---|---|---|
| `mode` | `auto` after `config init` | `off` captures only; `auto` reflects and promotes low-risk skills; `review` keeps every proposal pending until accepted. |
| `[reflection] tool_calls` | `40` | Reflection cadence counted in **tool events**, not turns. A working stretch triggers; a conversation that only talks never does. Lower = learns faster. |
| `[reflection] during_session` | `true` | `false` reflects only at session end. |
| `[reflection] timeout_seconds` | `180` | How long the reflector may run before the window is kept and retried (10-minute cool-down). |
| `[consolidation] every_tool_calls` | `250` | Same quantum for the curator pass that merges the library. Needs at least `min_skills` (3). |

**Recall — what the model sees**

| Setting | Default | What it does |
|---|---|---|
| `[index] session_start` | `true` | Set `false` to stop injecting the skill index. Capture and lifecycle keep running. |
| `[index] description_chars` | `100` | Per-line description budget in the index. |
| `[index] max_skills` | `30` | Cap on indexed skills, trusted first. |

**Lifecycle — what is kept**

| Setting | Default | What it does |
|---|---|---|
| `[skills.project] capacity` | `50` | Cap on Harness-managed project skills. Limits creation; never deletes. |
| `[skills.personal] capacity` | `20` | Same for personal skills — smaller because they load in every project. |

**Environment**

| Variable | What it does |
|---|---|
| `COPILOT_HARNESS_HOME` | Overrides the config/state directory. |
| `COPILOT_HARNESS_DEBUG` | `1` enables debug logging (overrides `[logging] debug`); inspect with `harness logs`. |

Full detail, including state locations and the custom-reflector contract, is in the
[reference](docs/reference.md#configuration-and-local-data).

## How it works

A learning pipeline runs beside Copilot CLI. Skills are plain native files, recalled by Copilot's own
name-and-description mechanism as if a human had written them. On top of that, each session opens with an
index of the skills Harness wrote.

```text
Copilot CLI hooks ─► CAP capture ─► SQLite (redacted) ─► REF reflect ─► promoter ─► .github/skills/
                                                             ▲              │
                              curator (consolidate) ─────────┘              ├─► ledger + versions
                              IDX index ◄── sessionStart ◄──────────────────┘
```

| Component | Role |
|---|---|
| **CAP** · capture | Hook-driven dumb pipe: records session start/end, prompts, successful and failed tool calls and agent stops, redacted before persistence. Also holds the trigger, which counts one thing — tool events. Nothing about content is judged here. |
| **REF** · reflect | Reads the conversation (from the transcript Copilot reports at each turn end; falls back to captured prompts) plus recent tool events and the existing skill index, then proposes `create`, `patch` or `ignore`. Proposes only — the default reflector runs with built-in tools excluded and has no write access. |
| **promoter** · validate·store | The only writer. Enforces size, name, path, ownership, frontmatter and secret-marker checks, then writes atomically. Attaches evidence IDs and source sessions from Harness's own store — the model is never trusted to invent provenance. Sensitive proposals stop here until accepted. |
| **IDX** · surface | Builds the session-start index of Harness-owned skills: name, scope, trust, truncated description. Empty library injects nothing. |
| **lifecycle** · trust | Skills move through `probation → trusted`, with `quarantine`, `archive`, `restore` and `rollback` as reversible side paths. Probation is Harness metadata, not a Copilot permission boundary. |
| **curator** · consolidate | The rarer whole-library pass: folds near-duplicates under one skill. Merges touching trusted or sensitive skills are held for `harness accept`. |
| **LED** · ledger | Per-skill append-only `.ledger.jsonl`: why each skill was born or changed, with evidence IDs. Kept out of the `SKILL.md` body so recall stays clean. |
| **feedback / export** | Explicit, version-bound `helpful`/`not-helpful` ratings, and hash-checked export of trusted skills with minimised provenance. Both are local; neither changes trust automatically. |

## Walkthrough: watching it learn

Everything lands on disk as plain files, so a demo is just opening them in order. To speed up the loop,
lower the threshold in `config.toml`:

```toml
[reflection]
tool_calls = 5
```

**1 · The pipeline running.** Work a few non-trivial turns. Once enough tool events accumulate, a detached
background reflection starts at turn end and does not block your session.

```powershell
harness status               # mode, latest session, skill counts, pending candidates, last reflection
harness logs -n 50           # with debug logging on: hooks received, why a reflection did or didn't start
```

**2 · A skill is born.** A new folder appears under `.github/skills/` (project) or `~/.copilot/skills/`
(personal).

```text
.github/skills/<name>/
  SKILL.md          # the skill itself — native Copilot format, nothing proprietary
  .sidecar.json     # trust state, provenance, version, validation result
  .ledger.jsonl     # why it was born / changed (append-only)
```

**3 · Review it.** Low-risk skills are live in probation; sensitive ones wait.

```powershell
harness proposals                          # candidates awaiting review
harness inspect <proposal-id> --proposal   # read before you accept
harness accept <proposal-id>               # records you as approver
harness skills --scope project             # state, usage ("used 3x"), feedback counts
harness inspect build-workflow
```

**4 · An update, not a duplicate.** Hit the same scenario again and the reflector proposes a `patch` to the
existing skill; the previous body is versioned and the skill returns to probation.

```powershell
harness rollback build-workflow            # restore a saved, digest-verified body
```

**5 · The next session opens knowing.** The `sessionStart` hook injects the skill index, so the library is in
front of the model whether or not Copilot's own recall would have surfaced it.

**6 · Validate before you trust.** Replay task cases against the new revision and its predecessor. This never
changes the skill; it exits non-zero on a regression and warns about likely conflicts with other skills.

```powershell
harness shadow build-workflow --cases .\evals\shadow
harness shadow --proposal <proposal-id>    # evaluate a pending proposal before accepting it
```

**7 · Trust, share, retire.** After review you decide what the skill is worth.

```powershell
harness feedback build-workflow --rating helpful
harness trust build-workflow --scope project
harness govern build-workflow --owner platform-team --criticality medium --expires 2027-01-01
harness export build-workflow --scope project --to .\shared-skills\skills
harness verify .\shared-skills\skills\build-workflow     # integrity + expiry check, not publisher identity
harness quarantine build-workflow --reason "Investigate incorrect instruction"
harness archive build-workflow             # reversible: harness restore build-workflow
```

**8 · Share through review.** Scaffold a shared repo whose CI runs `harness audit` (hash, expiry, secrets,
injection phrases, size, duplicates) and whose pull requests need
a code owner. Consumers import into a pending proposal; nothing installs until they accept it.

```powershell
harness shared-init ..\shared-skills                   # workflow, CODEOWNERS, PR template, README
harness import ..\shared-skills\skills\build-workflow  # verifies, then creates a pending proposal
harness share build-workflow --from personal --to project   # same review gate across local scopes
```

**9 · Yours are never touched.** Anything without Harness ownership metadata is invisible to the promoter and
lifecycle commands.

## Commands

```text
harness status | config [init] | doctor | logs [-n N]
harness learn [session-id] [--now]        harness consolidate [--scope S]
harness proposals [--all]                 harness inspect <name|proposal-id> [--proposal]
harness accept|reject <proposal-id>       harness skills [--scope S] [--unused-days N]
harness trust|quarantine|unquarantine|archive|restore|rollback <skill> [--scope S]
harness feedback <skill> --rating helpful|not-helpful
harness shadow <skill> | --proposal <id> [--baseline-version N] [--cases DIR]
harness govern <skill> --owner O --criticality C --expires YYYY-MM-DD
harness export <skill> --scope S --to <dir>        harness verify <dir>
harness import <dir> [--scope S]          harness share <skill> --from S --to S
harness shared-init <dir>                 harness audit <dir> [--policy FILE]
harness sign <dir> --key K                harness verify-signature <dir> --allowed-signers F
harness bundle <dir> --out FILE           harness verify-attestation <bundle> [--repo O/N]
harness eval [--only NAME]
```

Details and examples: [docs/reference.md](docs/reference.md).

## Limitations

- Reflection starts at turn end once the threshold is met, then at session end. Below-threshold short
  sessions need `harness learn --now`.
- Harness cannot tell whether a skill was *followed* or *helpful*. Load counts come from Copilot's
  undocumented `skill.invoked` transcript event and could change; ratings are self-reported and never change
  trust or prune anything.
- Shadow evaluation runs each hand-written case once per side through a live model, so results vary and a
  pass is evidence, not proof. Conflict detection is a word-overlap heuristic.
- Sensitive-content classification is keyword-based, not a security review. Probation does not stop Copilot
  loading a skill — don't put unreviewed high-impact instructions in one.
- Exports are unsigned by default. Optional SSH-key signatures (`harness sign`) prove a manifest came from a key in
  your `allowed_signers`, but the checks are advisory aids: the pull-request review in the shared repo is the
  real control. Policy files are plain JSON checked in with the skills, so protect them with code owners too.
- Not yet available: an organisation marketplace and a dashboard. Attestation needs GitHub and the `gh` CLI, and
  has only been unit-tested with a mocked `gh`, not against a live release. The
  detached reflection process still needs broader live cross-platform validation.

## Roadmap

**Now (v0.1):** local capture and reflection, session-start index, consolidation, probation/trust gates,
provenance, versioning, rollback, quarantine, feedback, shadow evaluation, governance metadata, hash-checked
export/import, a shared-repo scaffold with CI verification, and CI on Windows, Linux and macOS.

**Next — harden sharing.** Diffs of changed skills in CI, catalogue collision checks, more shadow cases and
repeated runs, and live validation of detached reflection on
macOS and Linux.

**Then — organisation rollout.** An organisation marketplace pinned by the policy file, and org-tier labels in the
session-start index.

**Later.** A dashboard and any shared telemetry — only once the governance model is agreed.
See the [enterprise promotion design](docs/enterprise.md).

## Documentation

- [Reference](docs/reference.md) — commands, configuration, hooks, proposal format, debugging, eval
- [Architecture](docs/architecture.md)
- [Skill lifecycle](docs/lifecycle.md)
- [Security](docs/security.md)
- [Enterprise skill promotion design](docs/enterprise.md)
- [Publishing (marketplace, releases, CI)](docs/publishing.md)

## Development

```powershell
$env:PYTHONPATH = "src"
py -m unittest discover -s tests -v      # PYTHONPATH=src python3 -m unittest ... on Linux/macOS
```

## Acknowledgements

Inspired by the architecture and learning lifecycle of
[tigerless-labs/autoharness](https://github.com/tigerless-labs/autoharness), adapted for GitHub Copilot CLI's
documented hooks and native Agent Skills. Not affiliated with Tigerless Labs.

## License

[MIT](LICENSE)
