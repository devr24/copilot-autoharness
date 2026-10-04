# Copilot Harness

Copilot Harness is a local-first, self-learning skills layer for GitHub Copilot CLI. It captures selected session-hook events, asks a reflector to distill reusable engineering lessons, and turns validated lessons into native Copilot Agent Skills.

The goal is a passive learning loop: do normal engineering work with Copilot; as the session accumulates enough real work, Harness reflects in the background on the actual conversation (your prompts and Copilot's replies) plus the tool activity, and either creates a skill, proposes an update, or decides there is nothing worth keeping.

> **Current MVP:** reflection reads the session conversation from the transcript Copilot reports at each turn end, and runs mid-session in a background process once enough new tool activity accumulates (default 40 tool events), plus a final pass at session end. Conversation-only sessions with no tool work never trigger reflection. Skill use is recorded from the transcript's skill.invoked events (verified live); usage-based retirement is not yet implemented.

## What it does

- **Captures locally:** Copilot CLI hooks record session start/end, submitted prompts, successful and failed tool calls, and agent-stop events in a local SQLite database.
- **Redacts before persistence:** common API tokens, passwords, connection-string secrets, AWS keys, URL credentials, private keys, and configured-looking environment secrets are filtered from captured values. Redaction is best-effort, not a guarantee.
- **Learns from the conversation, not just tool calls:** at every turn end Copilot reports its session transcript path; Harness reads the user and assistant messages (redacted, bounded, newest-first within a budget, always keeping the opening request) and gives them to the reflector as the primary evidence, alongside recent tool events. If the transcript is unavailable it falls back to captured prompts.
- **Learns automatically after opt-in:** `harness config init` enables `auto` mode and configures Copilot CLI as the reflector. Each time 40 new relevant tool events accumulate (configurable), a detached background process reflects on that window while you keep working; a final pass runs at session end. Each reflection consumes its window, so activity is never analyzed twice.
- **Handles subagents, resumes and forks:** subagent activity is written inline in the same transcript with a top-level `agentId`; those records are skipped (their prompts come from the parent agent, which relays the results). A resumed session keeps its session ID and is windowed by the existing mark. Messages of 40+ characters already reflected on in the same directory are fingerprinted (SHA-256, stored locally) and dropped, so history copied into a forked session is not learned twice. Verified on Copilot CLI 1.0.92 for subagents and resume; `/fork` is interactive-only, so fork handling is covered by tests with a simulated copied transcript, not a live fork.
- **Learns on demand:** `harness learn --now` reflects on new activity immediately, ignoring the threshold. From inside Copilot CLI you can run it with `!harness learn --now`.
- **Creates native skills:** accepted or automatically promoted project skills are written to `.github/skills/<name>/`; personal skills use `~/.copilot/skills/<name>/`.
- **Avoids duplicates where possible:** the reflector sees the full compact index of Harness-owned skills (name, scope, trust, description) plus the full text of the most relevant ones, and can propose `patch` instead of `create`.
- **Injects a skill index at session start:** the `sessionStart` hook returns `additionalContext` listing learned skills (trusted first, capped). Opt out with `[index] session_start = false`. Verified live on Copilot CLI 1.0.92: the model sees the list.
- **Consolidates:** every 250 new tool events (and at least 3 skills in a scope) a curator reflection proposes merging near-duplicate skills. Absorbed skills are archived (not deleted, `merged_into` recorded, restorable); the kept skill returns to probation. Merges touching trusted or sensitive skills are held for `harness accept`. Run manually with `harness consolidate`.
- **Keeps humans in control of risky knowledge:** low-risk auto-mode skills enter probation. Sensitive candidates—such as production, deployment, IAM, security, infrastructure, secrets, data migration, or destructive guidance—are held for explicit approval.
- **Protects human-owned skills:** create will not overwrite an existing skill. Patch, trust, quarantine, archive, restore, and rollback require Harness ownership metadata.
- **Tracks provenance and versions:** each skill records its source session/events, reason, local creator, repository, Harness version, and validation result. Previous skill bodies are versioned outside Copilot's loadable skill directory and protected with SHA-256 digests.
- **Supports recovery:** skills can be trusted, quarantined out of recall, restored, archived, or rolled back to an earlier version.
- **Needs no daemon:** hooks start bounded Harness processes; there is no always-running service.

## What it does not do yet

- It does not reflect after every tool event: it checks at turn end and starts reflection only after the configured threshold, then reflects on eligible remaining work at session end. Below-threshold short sessions require `harness learn --now`.
- It does not infer whether a skill was followed or helpful. You can explicitly record local feedback with `harness feedback`, but ratings are self-reported and do not affect trust, promotion, or pruning. Copilot's `skill.invoked` transcript signal is undocumented and could change between versions.
- It does not detect contradictory guidance beyond what the consolidation reflector notices.
- It does not provide shadow-mode outcome evaluation, a dashboard, hosted service, team or enterprise promotion workflow (a design is in `docs/enterprise.md`), or signed corporate skills. Plugin distribution through a published marketplace is described in `docs/publishing.md`.
- Sensitive-content detection is heuristic keyword classification, not semantic security review. Low-risk labels do not guarantee a skill is safe.

## Requirements

- Python 3.11 or later.
- GitHub Copilot CLI installed and authenticated.
- The `harness` command available on `PATH` in the environment used by Copilot CLI hooks.

Harness uses only Python's standard library at runtime.

## Install and opt in

From the project checkout, install the CLI in editable mode:

```powershell
py -m pip install -e .
harness --version
```

On Linux or macOS, use `python3 -m pip install -e .`.

Initialize Harness configuration:

```powershell
harness config init
harness status
```

`config init` is the explicit opt-in to automatic learning. It creates a default `auto` configuration with a Copilot CLI reflector with all built-in tools excluded. Without a configuration file, Harness stays in capture-only `off` mode.

**Data handling:** reflection sends the bounded, redacted conversation text, tool-event evidence and a sample of Harness-managed skills to the Copilot model provider. Conversation text can contain sensitive information that pattern-based redaction misses. Review your organization's data policy before enabling capture. The reflector runs in the background during sessions (and synchronously at session end) and may take up to the configured timeout (180 seconds by default).

## Install as a Copilot plugin (recommended)

The repository is itself a Copilot CLI plugin and a single-plugin marketplace (`plugin.json`, `.github/plugin/marketplace.json`). The plugin supplies the hook manifest, so you do not copy `hooks.json` into each repository. It does **not** install the `harness` executable: that still comes from `pip install`.

```powershell
py -m pip install .                       # provides harness.exe on PATH
harness config init                       # opt in to auto mode
copilot plugin marketplace add "C:\path\to\copilot-harness"   # or OWNER/REPO once published
copilot plugin install copilot-harness@copilot-harness-marketplace
harness doctor                            # verify
```

A local-directory marketplace loads the plugin live (nothing is copied). Verified on Copilot CLI 1.0.92: after installing, hooks fired in a fresh repository with no `.github/hooks` file. Do not use both the plugin and a repository hook manifest; `harness doctor` warns about double installation. Copilot has deprecated direct `copilot plugin install <path>`; use the marketplace route. To remove: `copilot plugin uninstall copilot-harness` and `copilot plugin marketplace remove copilot-harness-marketplace`.

`harness doctor` checks that `harness` resolves to a real executable (not a `.cmd` shim on Windows), that `copilot` is on PATH, that hooks are installed (plugin or repository manifest) and that configuration exists.
## Enable hooks in a repository (manual alternative)

From the repository where you want Harness to learn, copy the provided hook manifest into Copilot's repository hook directory. For example, if this project is checked out at `C:\path\to\copilot-harness`:

```powershell
New-Item -ItemType Directory -Force .github\hooks | Out-Null
Copy-Item "C:\path\to\copilot-harness\hooks\hooks.json" `
  .github\hooks\copilot-harness.json
```

The manifest invokes `harness hook` for:

| Copilot event | Harness use |
|---|---|
| `sessionStart` | Records session identity and working directory. |
| `userPromptSubmitted` | Captures submitted prompt evidence. |
| `postToolUse` | Captures successful tool activity. |
| `postToolUseFailure` | Captures failed tool activity. |
| `agentStop` | Records the end of an agent turn, captures the session transcript path, and in `auto` mode starts a background reflection if the threshold is met. |
| `sessionEnd` | Records session end and, in `auto` mode, reflects on any remaining window that meets the threshold. |

Start a fresh Copilot CLI session in that repository. Confirm capture with:

```powershell
harness status
```

The hook `exec` is spawned without a shell, so `harness` must resolve to a real executable (`harness.exe` from `pip install`); a `.cmd` shim fails with `spawn harness ENOENT`.

Hooks only work where the manifest is installed and the `harness` executable resolves correctly. You must install the manifest separately in each repository you want to capture.

## Learning modes and behavior

The `mode` setting in the Harness config supports:

| Mode | Behavior |
|---|---|
| `off` | Capture events but do not reflect or promote skills. This is the behavior when no config file exists. |
| `auto` | Automatically reflect eligible activity during and at the end of sessions. Promote low-risk create/patch proposals into probation; hold sensitive proposals for approval. This is selected by `harness config init`. |
| `review` | Reflect on eligible sessions and keep proposals pending until accepted or rejected. |

The default reflection threshold is **40 new relevant tool events** since the last reflection. Lower it to learn from shorter work:

```toml
[reflection]
tool_calls = 10
during_session = true   # set false to reflect only at session end
```

How triggering works:

- The threshold counts tool events, not conversation turns, so a session that only talks never triggers reflection.
- At each `agentStop` (end of a turn) in `auto` mode, if the threshold is met, Harness starts a detached `harness reflect-session` process so your session is not blocked. Outcomes are appended to `reflection.log` in the Harness home directory, and `harness status` shows the latest line.
- A per-session lock prevents overlapping reflections. If a reflection fails (for example the reflector is unavailable or times out), the window is kept and retried later, with a 10-minute cool-down for mid-session retries.
- If a background reflection is still running when the session ends, the session-end pass skips it; below-threshold leftovers are not reflected automatically. Use `harness learn --now` to capture them.
- Background reflections are detached, so they keep running after Copilot CLI exits (verified live: a reflection started at a turn end completed after the CLI had quit). If a process is killed anyway, its stale lock expires after the reflection timeout plus 60 seconds.

Low-risk skills are written into Copilot's native skill directory with state `probation`. **Probation is Harness metadata, not a Copilot permission boundary:** Copilot may discover and use the skill normally. `trust` is also Harness metadata and does not grant the skill additional execution permissions.

## Commands

```text
harness --help
harness --version
harness status
harness config
harness config init
harness learn [session-id] [--now]
harness proposals [--all]
harness inspect <proposal-id> --proposal
harness inspect <skill-name> [--scope project|personal]
harness feedback <skill-name> --rating helpful|not-helpful [--scope project|personal]
harness export <skill-name> --scope project|personal --to <directory>
harness verify <exported-skill-directory>
harness accept <proposal-id>
harness reject <proposal-id>
harness skills [--scope project|personal]
harness trust <skill-name> [--scope project|personal]
harness quarantine <skill-name> --reason "<reason>" [--scope project|personal]
harness unquarantine <skill-name> [--scope project|personal]
harness archive <skill-name> [--scope project|personal]
harness restore <skill-name> [--scope project|personal]
harness rollback <skill-name> [--version N] [--scope project|personal]
```

Examples:

```powershell
# Show state for the current working directory.
harness status

# Reflect on the latest captured session (or specify a session ID).
harness learn
harness learn --now
harness learn 01234567-89ab-cdef-0123-456789abcdef

# Score the configured reflector against replayable cases (run from the checkout).
harness eval
harness eval --only injection

# Merge near-duplicate skills (auto-applies in auto mode, else leaves pending).
harness consolidate --scope project

# Inspect, then approve or reject a sensitive/review-mode proposal.
harness proposals
harness inspect <proposal-id> --proposal
harness accept <proposal-id>
# or:
harness reject <proposal-id>

# Review and manage learned project skills.
harness skills --scope project   # includes usage: "used 3x, last 2026-10-04" or "never used"
harness feedback build-workflow --scope project --rating helpful
harness feedback build-workflow --scope project --rating not-helpful
harness trust build-workflow --scope project
harness export build-workflow --scope project --to .\shared-skills\skills
harness verify .\shared-skills\skills\build-workflow
harness inspect build-workflow --scope project
harness quarantine build-workflow --scope project --reason "Investigate incorrect instruction"
harness unquarantine build-workflow --scope project
harness rollback build-workflow --scope project
```

Feedback is an explicit, local-only signal attached to the current skill version. It records only the skill name, scope, version, rating, working directory, and timestamp—no explanation or conversation text. Ratings are shown by `harness skills`; they are not treated as proof of quality and never automatically change a skill's trust state.

`harness accept` records the current local account name as the approver. Sensitive candidates are promoted only after this explicit acceptance. Low-risk proposals in `review` mode are also promoted on acceptance. Newly created and changed skills enter probation; use `harness trust` after review to mark one trusted.

Quarantine moves a skill out of Copilot's loadable skills folder into Harness's quarantine storage without deleting its content or history. Unquarantine restores it to probation. Archive/restore is a separate reversible lifecycle operation. Rollback restores a saved body, verifies its SHA-256 digest, records the rollback, and returns the live skill to probation. If a skill has several versions, pass `--version N` to select a specific older version.

## Configuration and local data

Default configuration and state locations:

| Platform | Config | SQLite state |
|---|---|---|
| Windows | `%LOCALAPPDATA%\copilot-harness\config.toml` | `%LOCALAPPDATA%\copilot-harness\state.sqlite3` |
| Linux/macOS | `$XDG_STATE_HOME/copilot-harness/config.toml` (defaults to `~/.local/state/copilot-harness/config.toml`) | `$XDG_STATE_HOME/copilot-harness/state.sqlite3` |

Set `COPILOT_HARNESS_HOME` to override the Harness config/state directory. `harness config` prints the active configuration location and mode.

The generated config includes:

```toml
mode = "auto"

[reflection]
tool_calls = 40
during_session = true
timeout_seconds = 180
# A platform-specific Copilot CLI command is generated here.

[skills.project]
capacity = 50

[skills.personal]
capacity = 20
```

Capacities limit the number of Harness-managed skills created in each scope; they do not delete skills. You may edit `config.toml` to change the mode, thresholds, timeout, capacities, or reflection command. A custom reflection command receives the reflection prompt on stdin and must print one JSON object to stdout.

Skill storage:

| Data | Project scope | Personal scope |
|---|---|---|
| Loadable skills | `.github\skills\<name>\` | `~\.copilot\skills\<name>\` |
| Archived skills | `.github\copilot-harness\archive\<name>\` | `~\.copilot\copilot-harness\archive\<name>\` |
| Quarantined skills | `.github\copilot-harness\quarantine\<name>\` | `~\.copilot\copilot-harness\quarantine\<name>\` |
| Version history | `.github\copilot-harness\versions\<name>\<version>\` | `~\.copilot\copilot-harness\versions\<name>\<version>\` |

Each live Harness-owned skill contains `SKILL.md`, `.sidecar.json` metadata, and an append-only `.ledger.jsonl` history. Harness never automatically commits session evidence or skill files.

## Index and consolidation settings

```toml
[index]
session_start = true
description_chars = 100
max_skills = 30

[consolidation]
enabled = true
every_tool_calls = 250
min_skills = 3

[logging]
debug = false
```

## Proposal format

The reflector returns one JSON object. Harness validates the result and attaches evidence IDs and source-session IDs from its own event store; it does not trust the model to invent provenance.

Create example:

```json
{
  "action": "create",
  "scope": "project",
  "name": "build-workflow",
  "description": "Build and test this repository reliably.",
  "reason": "A repeatable, non-obvious validation sequence was discovered.",
  "confidence": 0.9,
  "skill_md": "---\nname: build-workflow\ndescription: Build and test this repository reliably.\n---\n\n# Build workflow\n\nRun the verified repository checks.\n",
  "target_name": ""
}
```

The `action` may be `create`, `patch`, or `ignore`. A patch must name an existing Harness-owned skill through `target_name`; its name must match that target. The skill content must include matching YAML `name` and `description` frontmatter. The promoter enforces size, name, path, ownership, frontmatter, and secret-marker checks before writing.

## Security and trust boundaries

- Hook data may include prompts, tool arguments/results, error messages, and local paths. It is redacted before local persistence, but **redaction is best-effort**.
- The reflector receives stored redacted event data and existing Harness-owned skill content. The default Copilot command runs with its built-in tools excluded, but the model provider still receives the prompt content.
- Session-end reflection runs synchronously within the hook command and is configured with a 180-second timeout. A slow or failed reflector can delay or fail to create a learned skill; it does not silently create a success-shaped proposal.
- A deterministic promoter is the only code that writes skill files. The reflector cannot directly modify the skill library.
- Create refuses to overwrite existing skills. Lifecycle operations verify Harness ownership metadata and reject symlinked managed paths.
- Sensitive-risk classification uses keywords and is not a comprehensive security detector. Treat it as a conservative gate, not a guarantee; inspect all learned guidance, especially before trusting it.
- Skills in probation can still be loaded by Copilot. Do not put unreviewed high-impact instructions in a skill and assume the probation label blocks execution.
- No automatic usage-based deletion occurs. Archive and quarantine preserve files for recovery.

## Development and tests

Run the standard-library test suite from the project root:

```powershell
$env:PYTHONPATH = "src"
py -m unittest discover -s tests -v
```

On Linux or macOS:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Tests cover hook normalization, redaction, state/config migration, reflection and auto-promotion, sensitive-candidate gating, ownership protection, skill validation, provenance, versioning, rollback integrity, quarantine, and archive/restore.

## Debug logging

Off by default. Enable it to see exactly when hooks fire and what Harness decided:

```toml
[logging]
debug = true
```

or set `COPILOT_HARNESS_DEBUG=1` in the environment (it overrides the config in both directions, and works even when `config.toml` is invalid). Then:

```powershell
harness status      # shows "Debug logging: on (<path>\debug.log)"
harness logs -n 100 # tail the log
```

The log (`debug.log` in the Harness home, rotated at about 1 MB to `debug.log.1`) records, with timestamp and process ID: each hook received and stored (event, session, tool name, outcome, directory, duration), the session-start index injection size, why a mid-session reflection did or did not start (mode, threshold count, cooldown, lock), the background process spawn, reflection start/finish/outcome, and full tracebacks for failed commands. It never records prompt text, tool arguments, tool output or skill bodies. Logging never raises: a logging failure cannot break a Copilot hook. Separately, `reflection.log` always records reflection outcomes.

Typical use: hooks seem not to fire, so check `harness logs` for `hook ... received` after a Copilot turn. If nothing appears, run `harness doctor` (plugin/manifest or `harness.exe` problems); Copilot's own `--log-level debug --log-dir` output shows hook spawn errors such as `spawn harness ENOENT`.

## Reflection quality eval

`harness eval` replays cases from `evals/cases/*.json` through the same prompt builder and the configured reflector, then scores the answers. Each case holds a conversation, optional tool events and existing skills, plus expectations: `action` (a value or list), `patch_target`, `must_include` and `must_exclude` strings. Scoring also validates the skill and fails on secret-like output. Shipped cases cover a repeatable workflow (create), a trivial task (ignore), a secret in the conversation (no leak), prompt injection in tool output (ignore) and a near-duplicate (patch the existing skill). Exit code is non-zero if any case fails. Run it after changing the prompt, the reflector command or upgrading Copilot CLI. It calls the live model, so it uses credits and results can vary between runs. Verified live: 5/5 on Copilot CLI 1.0.92. Add a case by dropping in another JSON file.

## Roadmap and references

**Current foundation:** local capture and reflection, a session-start Harness skill index, periodic consolidation, skill-load counts, explicit version-bound helpful/not-helpful feedback, provenance, trust gates, rollback and quarantine. Trusted skills can be exported with minimized provenance, then checked with `harness verify`; this verifies content integrity only, not publisher identity. `.github/workflows/ci.yml` runs the unit suite on Ubuntu, macOS and Windows with Python 3.11 and 3.13. The detached-reflection process still needs broader live cross-platform validation.

**Next: validate outcomes before automating trust.** Expand the replayable evaluation harness into a shadow mode that compares a candidate skill revision with its previous version on the same task cases. Report regressions and likely conflicts without changing the live skill. Use explicit feedback as a diagnostic signal, not as an automatic score or pruning trigger; skill-load records alone are not evidence of usefulness.

**After shadow evaluation: make exports reviewable and shareable.** Add a reusable CI/review template around the existing trusted-skill export and hash verification. Keep signing, organisation distribution, policy enforcement, dashboards, and any shared telemetry as later work, after the local feedback and regression loop is reliable and the governance model is defined.

Use `harness skills --unused-days N` only as a manual review aid; archiving stays explicit because Copilot's load signal is undocumented and loading does not establish value. See [the enterprise promotion design](docs/enterprise.md) for the proposed trust tiers and sharing safeguards.

Related design notes:

- [Architecture](docs/architecture.md)
- [Security](docs/security.md)
- [Skill lifecycle](docs/lifecycle.md)
- [Publishing to GitHub (marketplace, releases, CI)](docs/publishing.md)
- [Enterprise skill promotion design (organisation workflow not implemented)](docs/enterprise.md)

Inspired by the architecture and learning lifecycle of [AutoHarness](https://github.com/tigerless-labs/autoharness), adapted for GitHub Copilot CLI's documented hooks and native Agent Skills.
