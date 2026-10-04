# Enterprise skill promotion: design

Status: **organisation governance and distribution are design-only.** The local trusted-skill export
and unsigned hash verification in phase 1 are implemented; shared-repository review, signing, and
organisation workflows are not.

## Problem

Harness learns skills for one person in one repository. An organisation wants the good ones shared
(team → org), the bad ones kept out, and a record of who approved what. The risk is that a skill is
an instruction to an agent: a poisoned or sloppy skill shared org-wide is a supply-chain problem, not
just a quality problem.

## What the platform gives us (and what is unverified)

From the Copilot CLI plugin reference and docs reviewed so far:

- Plugins can be distributed through **marketplaces** (a Git repository with `marketplace.json`).
- Organisations or device management can **pin** plugins and marketplaces
  (`enabledPlugins`, `extraKnownMarketplaces`) so users cannot re-point or disable them locally.
- Hooks can be enforced by **policy** (`policy.d` on Windows).
- Skills load from `.github/skills` (project), `~/.copilot/skills` (personal) and from plugins.

Not established, and to be verified before relying on them:

- Whether Copilot CLI verifies any signature on plugins or skills. Assume it does **not**.
- How a managed marketplace behaves for users without access to the backing repository.
- Whether plugin-shipped skills are labelled in a way an index or audit can distinguish.

## Proposed model: four tiers

| Tier | Where it lives | Who can create | Gate |
|---|---|---|---|
| 1. Personal | `~/.copilot/skills` | Harness (auto) | Probation state, redaction, risk classifier |
| 2. Project | `.github/skills` in a repo | Harness (auto) or user | Probation → `harness trust` by the author |
| 3. Team candidate | Pull request to a shared skills repo | A human via `harness export` | Automated checks + one reviewer |
| 4. Organisation | Plugin in an org marketplace, pinned by policy | Publisher role | Two-person review + signature |

Nothing moves up a tier automatically. Auto-learning stops at tier 2, deliberately: the existing rule
that trusted skills are never auto-patched extends upward.

## Components

### 1. Export (`harness export <skill> --to <dir>`)

Implemented locally: `harness export <skill> --scope project|personal --to <dir>` writes
`<dir>/<name>/SKILL.md` and `provenance.json`. Export only accepts trusted Harness-managed
skills and validates the content again. The manifest contains the body SHA-256, skill and Harness
versions, export timestamp, and source-session count. It deliberately omits session IDs, user names,
local paths, repository names, conversation excerpts, and the reflector's free-form reason.

`harness verify <dir>/<name>` checks the manifest version, skill name, content validation, and body
hash. This is an **unsigned integrity check only**: because the manifest sits beside the skill, it
does not authenticate a publisher or protect against an attacker who can replace both files.

Export refuses anything that is not ready: the skill must be `trusted` and not quarantined, and it must
pass the existing validation and secret-marker checks again at export time.

### 2. Review pipeline (CI in the shared repo)

Run on every PR: `validate_skill`; secret/redaction scan; keyword risk classifier; a prompt-injection
check (flag instructions that mention exfiltration, disabling safeguards, fetching remote
instructions, or overriding other skills); size limits; name collision and near-duplicate detection
against existing org skills; a diff of any change to an existing skill. Findings block merge for
sensitive classes; they never auto-approve. `CODEOWNERS` routes security-relevant skills (deploy,
IAM, secrets, infrastructure) to a security reviewer. The classifier is keyword-based, so humans
remain the control, not the checks.

### 3. Signing and publisher authentication (not implemented)

Because Copilot may not verify signatures, verification has to happen where we control the code:

- **At publish:** the release job attests the built plugin (GitHub artifact attestations or
  Sigstore/cosign) bound to the repository, workflow and commit.
- **At install:** extend `harness verify` to check the attestation and every skill's SHA-256 against
  `provenance.json`; `harness doctor` can warn on mismatch. Hash-only verification is implemented,
  but attestation verification is not.
- **Real enforcement** comes from policy: pin the marketplace to a locked-down repository (branch
  protection, required reviews, restricted write access), so only reviewed commits can reach users.

### 4. Distribution

An org marketplace repository (`marketplace.json`) with one plugin per theme (for example
`acme-python-skills`). Versions are tagged; the policy pins the marketplace so rollout and rollback
are a repository operation. The org skills appear to Copilot as ordinary skills, so Harness's
session-start index should label them as organisation-approved and exclude them from consolidation:
Harness must never merge, patch, archive or "learn over" a skill it does not own (it already refuses
unmanaged skills).

### 5. Governance

- Roles: **author** (any developer), **reviewer** (team lead), **publisher** (platform/security).
- Revocation: remove from the marketplace and tag a release; ship a **deny list** in the plugin that
  `harness doctor` consults so a withdrawn skill is flagged on machines that still have it.
- Audit: every promotion is a PR, so authorship, reviewers, and diffs are in Git history.
- Ageing: tier 4 skills carry an owner and a review-by date; expired skills are reported, not
  silently removed.

### 6. Optional usage telemetry

Aggregate "skill loaded" counts would show which org skills matter, using the `skill.invoked`
signal Harness already records locally. Sharing it is a privacy decision (names and timestamps per
person are workplace monitoring in some jurisdictions). Default: **no collection**. If wanted,
aggregate counts per skill with no user or session identifiers, opt-in, documented to staff.

## Threat model

| Threat | Mitigation |
|---|---|
| Malicious or poisoned skill promoted (e.g. via prompt injection captured in a session) | Tier gating, injection checks, two-person review for tier 4, no automatic promotion |
| Secrets or customer data leaked into a shared skill | Redaction at capture, at export and in CI; provenance excludes conversation text; human review |
| Skill tampered with after review | Attestation + hash verification; protected marketplace repo |
| Compromised publisher account | Required reviews, 2FA, branch protection, signing from CI not from laptops |
| Stale or wrong org guidance | Owners, review-by dates, deny list, usage-based review (if telemetry is adopted) |
| Org skill silently overwritten by local learning | Harness never modifies unmanaged skills; org tier excluded from patch/merge |
| Skill hides unsafe tool permissions | Review checks `allowed-tools`-style fields; flag broad grants |

## Implementation phases

1. **Implemented:** trusted-skill export + privacy-minimized `provenance.json` + `harness verify` (unsigned hashes only). Local, no GitHub dependency.
2. CI workflow template for the shared repo (checks, `CODEOWNERS`, PR template).
3. Attestation/signing in the release workflow; verification in `verify`.
4. Org marketplace + policy pinning guide; deny list; labels in the session-start index.
5. Optional aggregate telemetry (only if the organisation asks for it).

## Decisions needed

1. **Who are the approvers?** One reviewer or two; is a security role mandatory for sensitive classes?
2. **Where does the shared repo live?** One org-wide repo or one per team; public, internal or private.
3. **Signing:** GitHub attestations (simplest in GitHub-only orgs) or Sigstore/cosign or an internal PKI?
4. **Enforcement:** will you use managed policy (MDM / org settings) to pin the marketplace, or rely
   on documentation? Without pinning, tier 4 is advisory.
5. **Telemetry:** none, or aggregate counts?
6. **Offline / air-gapped users:** is a mirrored marketplace needed?
7. **Scope:** is this for your own organisation first, or a product others will deploy?

The local export and hash-check phase is implemented without requiring answers to these questions.
