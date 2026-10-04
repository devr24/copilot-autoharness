# Enterprise skill promotion: design

Status: **local sharing mechanics are implemented; organisation distribution, signing and policy are
design-only.** Implemented: trusted-skill export and hash verification, optional SSH-key signatures
(`harness sign`), an organisation policy file (deny list, owner allowlist, criticality cap, required
governance and signatures), owner / criticality /
expiry metadata (`harness govern`), a shared-repository scaffold with CI verification and review
templates (`harness shared-init`), review-gated import and cross-scope sharing (`harness import`,
`harness share`), and shadow evaluation (`harness shadow`). Not implemented: build attestation,
organisation marketplace, telemetry.

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

`harness shared-init <dir>` scaffolds the repository: a `verify-skills` GitHub Actions workflow that runs
`harness verify` on every `skills/*` directory for PRs and pushes, a `CODEOWNERS` placeholder, a PR
template with a reviewer checklist, and a README describing contribute and consume steps. Branch
protection with required code-owner review is the real control and must be enabled by hand.

`harness govern <skill> --owner ... --criticality low|medium|high|critical --expires YYYY-MM-DD`
records ownership in the skill sidecar. Export requires complete, unexpired governance for sensitive
or high/critical skills, writes owner, criticality and expiry into the manifest, and `harness verify`
fails once the expiry date has passed, so CI flags stale shared skills. The manifest is unsigned, so
these fields are advisory against a malicious editor; PR review is what protects them.

Consumers run `harness import <dir>`, which re-verifies the hash and creates a *pending proposal*;
nothing is installed until `harness accept`, and accepted skills start in probation. `harness share
<skill> --from personal --to project` does the same across local scopes for a trusted skill.

`harness audit <dir>` runs these checks and exits 1 on any finding: hash/manifest verification and
expiry, policy (below), secret-like content, prompt-injection phrases (overriding instructions, revealing secrets,
piping downloads into a shell, hiding actions from the user, bypassing review, hidden bidirectional
characters), a 20 KB size limit, and near-duplicate descriptions between skills. The scaffolded
workflow runs `harness audit skills`. Still to build: diffs of changed skills and name collisions
against an existing org catalogue. The checks are heuristics, so humans remain the control. **Policy and signing.** `harness-policy.json` (scaffolded by `shared-init`, found in the current directory,
`.github/`, or beside the audited folder, or passed with `--policy`) holds `deny_names`, `deny_sha256`,
`allowed_owners`, `max_criticality`, `require_governance`, `require_signature` and an `allowed_signers`
path. It is enforced by `harness audit` (as `[policy]` findings) and by `harness import` (which refuses
before creating a proposal). Publishers sign with `harness sign <dir> --key <ssh-private-key>`, which uses
`ssh-keygen -Y sign` over `provenance.json` (the manifest contains the `SKILL.md` hash), and consumers check
with `harness verify-signature <dir> --allowed-signers <file>`; an `allowed_signers` file lists approved
identities and public keys in the standard OpenSSH format. This needs OpenSSH 8.2+ `ssh-keygen` on PATH.
Signatures prove key possession, not that the content is safe; revoke by removing a key or adding a hash
to the deny list.

The wider planned checks are:

Run on every PR: `validate_skill`; secret/redaction scan; keyword risk classifier; a prompt-injection
check (flag instructions that mention exfiltration, disabling safeguards, fetching remote
instructions, or overriding other skills); size limits; name collision and near-duplicate detection
against existing org skills; a diff of any change to an existing skill. Findings block merge for
sensitive classes; they never auto-approve. `CODEOWNERS` routes security-relevant skills (deploy,
IAM, secrets, infrastructure) to a security reviewer. The classifier is keyword-based, so humans
remain the control, not the checks.

### 2b. Shadow evaluation (`harness shadow`)

Before trusting or sharing a revision, `harness shadow <skill>` replays task cases from `evals/shadow/`
through the configured model command twice, once with the baseline (the previous saved version, a chosen
`--baseline-version`, or no skill) and once with the candidate (the live skill, or a pending proposal via
`--proposal`). Each answer is scored on `must_include` / `must_exclude`. A case that passed with the
baseline and fails with the candidate is a regression and makes the command exit non-zero. It also prints
heuristic conflict warnings (near-duplicate descriptions, opposing always/never lines against other managed
skills). It never writes to a skill. Each case runs once, model output varies, and cases are
hand-written, so treat a pass as evidence, not proof.

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
2. **Implemented (basic):** CI workflow template for the shared repo (`harness shared-init`: hash verification, `CODEOWNERS`, PR template), governance metadata and review-gated import. Richer CI checks remain (diffs of changed skills, catalogue collisions).
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
