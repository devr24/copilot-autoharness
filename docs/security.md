# Security and data handling

## Data flow

Hook payloads may contain prompts, tool arguments, results, errors, and local paths. Harness redacts common token, password, API-key, connection-string, AWS-key, URL-credential, and private-key formats, bounds nested values, and persists the result in a local SQLite database outside the repository. Redaction is best-effort and cannot guarantee that all sensitive information is removed.

After the user explicitly runs `harness config init`, a session-end hook sends a bounded, redacted event summary and existing Harness-managed skill text to GitHub Copilot CLI for reflection after the work threshold is met. Without a config file Harness remains in capture-only mode. The generated default command pipes the prompt on stdin to Copilot CLI with every built-in tool excluded and built-in MCP servers disabled. Verified live with `--allow-all` pre-approving everything: with no restrictions Copilot read a canary file, edited and created files and ran shell commands; with the Harness flags it did none of these and did not leak the canary. Verified on Windows (PowerShell) and with the POSIX `sh` command (run through Git's `sh` on Windows). Future Copilot versions may add tools, so re-run this check after upgrading. The model/provider still receives the content. Review your organization's data-handling policy before opting in. A custom reflector command receives the same data on stdin.

## Mutation boundaries

- Reflection produces proposals; it does not write skill files.
- Auto mode is the default. Review mode is available as an opt-in.
- New skills begin in probation, not trusted. Sensitive categories stay as candidates until a developer accepts them.
- Prior skill contents are versioned outside Copilot's loadable skill tree. `rollback` restores an earlier body and puts the skill back into probation.
- Version snapshots carry a SHA-256 digest; rollback verifies the digest and refuses a changed snapshot.
- `export` only accepts trusted managed skills and writes a minimized provenance manifest. `verify` checks the exported body hash, but the unsigned manifest does not authenticate its publisher and is not a substitute for review or signed releases.
- `quarantine` moves a managed skill out of the loadable tree while retaining its contents and audit trail.
- Create refuses to overwrite an existing skill. Patch, archive, and restore require a Harness ownership marker.
- Skill names are constrained slugs; symlinked skill roots and files are refused.
- Skills are validated and written under the native personal or project skill directory.
- Reflected-message fingerprints are truncated SHA-256 hashes; they do not store message text.
- Skill-use records contain only the skill name, scope and timestamp.
- Explicit feedback records contain the skill name, scope, version, rating, working directory and timestamp; they remain in the local Harness database and are not sent to the reflector.
- Debug logging (opt-in) records event names, session IDs, tool names, directories and decisions, but never prompt text, tool arguments or output. Session IDs and directory paths are still identifying; treat `debug.log` as local diagnostic data and do not attach it to public issues unreviewed.
- Archiving moves a managed skill; Harness does not automatically delete or prune skills.

Do not enable capture in sensitive environments without evaluating the data being collected and the local storage policy. Session data is not automatically committed to source control, but it remains on disk until removed by the user.
