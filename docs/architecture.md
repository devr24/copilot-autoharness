# MVP architecture

Copilot Harness is a local Python CLI invoked by Copilot CLI command hooks. Hooks capture normalized lifecycle events and append them to a user-local SQLite database. At `agentStop` Copilot supplies the session transcript path, which Harness stores. When the new-tool-event threshold is met, auto mode spawns a detached `harness reflect-session` process that reads the user/assistant text from the transcript (redacted and bounded), combines it with recent tool events, and reflects using a configured local command. A per-session lock, failure cool-down, and reflection marks keep reflections from overlapping or re-analyzing the same activity. `sessionEnd` runs a final pass when enough unreflected tool events remain; `harness learn --now` offers an explicit below-threshold pass on demand.

The reflection command returns a structured proposal. Harness provides existing Harness-owned skills to the reflector to encourage patching same-scenario skills rather than accumulating duplicates. Harness validates action, scope, name, description, frontmatter, size, paths, and evidence before storing the proposal. Auto mode sends low-risk proposals through the promoter; sensitive proposals remain candidates pending explicit approval. Review mode holds all proposals. The promoter is the only code path that writes skill files; it protects skills without a Harness ownership sidecar, writes native Agent Skills in probation, and records session, event, user, repository, validator, and proposal provenance. Each accepted revision has an external version snapshot and can be rolled back or quarantined out of Copilot's skill directory.

Project skills live under `.github/skills/`; personal skills live under `~/.copilot/skills/`. Harness is distributed as a local Copilot CLI plugin/marketplace and does not require a resident service. Promotion into a shared team or organisation skill library remains outside the MVP.

## Curator and index modules

- `index.py`: formats the compact skill index and builds sessionStart context.
- `curator.py`: consolidation prompt, merge validation, proposals, auto-apply and the periodic trigger.
- `skills.merge_skills`: applies a validated merge (patch kept skill, archive absorbed).

- `doctor.py`: installation checks (executable, copilot CLI, hooks via plugin or repo manifest, config).
- Packaging: `plugin.json` (legacy manifest pointing at `hooks/hooks.json`) and `.github/plugin/marketplace.json` (`source: .`).
- `usage.py`: finds idle non-trusted skills from recorded loads (`harness skills --unused-days N`).
- `evals.py`: loads `evals/cases`, builds prompts via `build_reflection_prompt`, runs the reflector and scores answers (`harness eval`).
- `debug.py`: opt-in `debug.log` (config `[logging] debug` or `COPILOT_HARNESS_DEBUG`), never raises, no session content; `harness logs` tails it.
- `transcript.py` skips subagent records (top-level `agentId`) and drops messages whose fingerprint is in `reflected_messages` (fork/resume dedupe).
