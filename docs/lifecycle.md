# Skill lifecycle in the MVP

1. Auto mode checks, at each `agentStop` and at `sessionEnd`, whether the new successful/failed tool event count since the last reflection meets the configured threshold.
2. If eligible, the reflector receives a bounded, redacted session summary and the existing managed skill library.
3. It proposes `create`, `patch`, or `ignore`; Harness attaches evidence IDs from its local event store.
4. Low-risk auto-mode proposals are validated and promoted to `probation`; sensitive proposals remain candidates pending explicit human approval. Review mode keeps every proposal pending.
5. Every create or patch records provenance and preserves a prior version snapshot. A developer can promote probation to trusted, quarantine a skill out of recall, restore it to probation, roll back to a previous version, archive, or restore.

Risk classification is conservative and keyword-based; it errs toward review and is not a semantic security guarantee. It should not replace human judgment.

`harness feedback <skill> --rating helpful|not-helpful` records an explicit local rating against the current managed skill version. Ratings are visible alongside load counts but do not alter trust, promotion, or archival state. They are diagnostic input only; Harness does not infer outcomes from a skill load.

Copilot's documented hook contract has no skill-loaded event, but the session transcript contains a `skill.invoked` record (name, path, source, plugin) each time a skill is loaded, verified live on Copilot CLI 1.0.92. Harness records these into `skill_usage` at turn end and session end, and `harness skills` shows use counts and last use. Loading is not the same as following or benefiting from a skill, and the record is undocumented, so Harness does not yet automatically retire or prune skills; use ``harness skills --unused-days N`` to list non-trusted skills idle for N days (suggestion only; archive manually).

## Consolidation and index

At `sessionStart` the hook returns `additionalContext` listing learned skills (trusted first, capped by `[index] max_skills`). The reflector also sees the full index plus the most relevant skill bodies. Every `[consolidation] every_tool_calls` new tool events (with at least `min_skills` skills in a scope), or via `harness consolidate`, a curator reflection proposes merges. Low-risk merges of non-trusted skills auto-apply in `auto` mode: the kept skill is patched (returns to probation), absorbed skills are archived with `merged_into` recorded and are restorable with `harness restore`. Trusted or sensitive merges stay pending for `harness accept`. Both paths were verified live against Copilot CLI 1.0.92.


## Installation paths

Preferred: `pip install` for the executable, then install the plugin from the local marketplace (hooks come from the plugin). Alternative: copy `hooks/hooks.json` into `.github/hooks`. Never both. Verify with `harness doctor`.

## Subagents, resume and fork

Subagent turns share the parent transcript and are excluded from conversation evidence. Resume appends to the same session. A fork is assumed to carry copied history, so reflected messages are fingerprinted per working directory and skipped on later reflections. Very short messages are never fingerprinted. Tool events are not de-duplicated because each session's hooks record only its own activity.

## Evaluating the reflector

`harness eval` is the regression check for the learning step: run it after prompt, reflector or Copilot CLI changes.
