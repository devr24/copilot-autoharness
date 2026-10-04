import argparse
import getpass
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .debug import DEBUG_LOG_NAME, debug_enabled, debug_exception, debug_log, tail_debug_log
from .audit import audit_skills
from .attest import build_bundle, verify_attestation
from .policy import enforce_policy, find_policy, load_policy, sign_export, verify_signature
from .background import (
    last_reflection_log,
    log_reflection,
    reflect_session,
    should_reflect_mid_session,
    spawn_background_reflection,
)
from .config import default_config_text, load_config
from .curator import consolidate
from .doctor import run_checks
from .evals import run_eval
from .errors import HarnessError
from .hooks import handle_hook
from .index import session_start_context
from .reflection import learn
from .skills import (
    export_skill,
    import_proposal,
    inspect_skill,
    list_skills,
    managed_skill_context,
    merge_skills,
    move_skill,
    promote,
    classify_risk,
    quarantine_skill,
    read_version,
    restore_quarantined_skill,
    rollback_skill,
    set_governance,
    share_proposal,
    trust_skill,
    verify_export,
)
from .shadow import detect_conflicts, load_task_cases, run_shadow
from .sharing import scaffold_shared_repo
from .storage import Storage
from .usage import find_unused
from .transcript import read_skill_invocations


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness",
        description="Capture engineering lessons and manage native Copilot Agent Skills.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Show capture and configuration status.")
    commands.add_parser("doctor", help="Check that hooks, the harness executable and config are set up.")
    logs_parser = commands.add_parser("logs", help="Show the tail of the debug log.")
    logs_parser.add_argument("-n", "--lines", type=int, default=50)
    eval_parser = commands.add_parser("eval", help="Score the configured reflector against replayable eval cases.")
    eval_parser.add_argument("--cases", help="Directory of eval case JSON files (default: evals/cases).")
    eval_parser.add_argument("--only", help="Run only cases whose name contains this text.")
    hook_parser = commands.add_parser("hook", help="Read one Copilot hook payload from stdin.")
    hook_parser.add_argument(
        "--event",
        choices=(
            "sessionStart", "userPromptSubmitted", "postToolUse",
            "postToolUseFailure", "agentStop", "sessionEnd",
        ),
        help="Event name supplied by the hook configuration.",
    )
    config_parser = commands.add_parser("config", help="Inspect or initialize configuration.")
    config_parser.add_subparsers(dest="config_action").add_parser(
        "init", help="Write a conservative default config file."
    )
    learn_parser = commands.add_parser("learn", help="Reflect on a captured session and create a proposal.")
    learn_parser.add_argument("session_id", nargs="?", help="Captured session ID; defaults to the latest here.")
    learn_parser.add_argument(
        "--now",
        action="store_true",
        help="Reflect on new activity immediately, ignoring the tool-call threshold.",
    )
    consolidate_parser = commands.add_parser(
        "consolidate", help="Find same-scenario skills and merge (auto) or propose merging them."
    )
    consolidate_parser.add_argument("--scope", choices=("project", "personal"))
    background_parser = commands.add_parser("reflect-session", help=argparse.SUPPRESS)
    background_parser.add_argument("session_id")
    background_parser.add_argument("--cwd", required=True)
    proposal_parser = commands.add_parser("proposals", help="List pending proposals.")
    proposal_parser.add_argument("--all", action="store_true", help="Include resolved proposals.")
    inspect_parser = commands.add_parser("inspect", help="Inspect a proposal or skill.")
    inspect_parser.add_argument("name")
    inspect_parser.add_argument("--scope", choices=("project", "personal"), default="project")
    inspect_parser.add_argument("--proposal", action="store_true", help="Inspect a proposal ID.")
    feedback_parser = commands.add_parser(
        "feedback", help="Record whether a Harness-managed skill helped (stored locally)."
    )
    feedback_parser.add_argument("name")
    feedback_parser.add_argument("--rating", choices=("helpful", "not-helpful"), required=True)
    feedback_parser.add_argument("--scope", choices=("project", "personal"), default="project")
    export_parser = commands.add_parser(
        "export", help="Export a trusted skill with privacy-minimized provenance."
    )
    export_parser.add_argument("name")
    export_parser.add_argument("--scope", choices=("project", "personal"), default="project")
    export_parser.add_argument("--to", type=Path, required=True, help="Directory that will contain the exported skill.")
    verify_parser = commands.add_parser("verify", help="Verify a skill export's content hash.")
    verify_parser.add_argument("directory", type=Path, help="Exported skill directory.")
    audit_parser = commands.add_parser(
        "audit", help="Check a directory of exported skills for tampering, secrets, injection and duplicates."
    )
    audit_parser.add_argument("directory", type=Path, help="Directory containing exported skill folders.")
    audit_parser.add_argument("--policy", type=Path, help="Policy file (default: harness-policy.json found in the current or parent directory).")
    bundle_parser = commands.add_parser("bundle", help="Build a deterministic .tar.gz of a skills directory for release.")
    bundle_parser.add_argument("directory", type=Path)
    bundle_parser.add_argument("--out", type=Path, required=True)
    attest_parser = commands.add_parser(
        "verify-attestation", help="Verify a skills bundle's GitHub build-provenance attestation (needs gh)."
    )
    attest_parser.add_argument("bundle", type=Path)
    attest_parser.add_argument("--repo", help="Trusted owner/name (default: trusted_repo from policy).")
    attest_parser.add_argument("--workflow", help="Trusted signer workflow (default: trusted_workflow from policy).")
    attest_parser.add_argument("--policy", type=Path)
    sign_parser = commands.add_parser("sign", help="Sign a skill export's provenance.json with an SSH key.")
    sign_parser.add_argument("directory", type=Path)
    sign_parser.add_argument("--key", type=Path, required=True, help="SSH private key (ssh-keygen -t ed25519).")
    verify_sig_parser = commands.add_parser("verify-signature", help="Verify an export signature against an allowed_signers file.")
    verify_sig_parser.add_argument("directory", type=Path)
    verify_sig_parser.add_argument("--allowed-signers", type=Path, required=True)
    govern_parser = commands.add_parser(
        "govern", help="Record owner, criticality and review expiry for a skill (required to share risky skills)."
    )
    govern_parser.add_argument("name")
    govern_parser.add_argument("--scope", choices=("project", "personal"), default="project")
    govern_parser.add_argument("--owner", required=True)
    govern_parser.add_argument("--criticality", choices=("low", "medium", "high", "critical"), required=True)
    govern_parser.add_argument("--expires", required=True, metavar="YYYY-MM-DD")
    share_parser = commands.add_parser(
        "share", help="Propose copying a trusted skill into another scope (pending human review)."
    )
    share_parser.add_argument("name")
    share_parser.add_argument("--from", dest="source", choices=("project", "personal"), required=True)
    share_parser.add_argument("--to", dest="target", choices=("project", "personal"), required=True)
    import_parser = commands.add_parser(
        "import", help="Verify a skill export and create a pending proposal (never installs directly)."
    )
    import_parser.add_argument("directory", type=Path)
    import_parser.add_argument("--scope", choices=("project", "personal"), default="project")
    import_parser.add_argument("--policy", type=Path, help="Policy file (default: harness-policy.json in the current directory).")
    shared_init_parser = commands.add_parser(
        "shared-init", help="Scaffold a shared-skills repository with CI verification and review templates."
    )
    shared_init_parser.add_argument("directory", type=Path)
    shadow_parser = commands.add_parser(
        "shadow", help="Compare a skill with a baseline on task cases without changing it."
    )
    shadow_parser.add_argument("name", nargs="?", help="Live skill to evaluate (omit with --proposal).")
    shadow_parser.add_argument("--scope", choices=("project", "personal"), default="project")
    shadow_parser.add_argument("--proposal", metavar="ID", help="Evaluate a pending proposal instead of the live skill.")
    shadow_parser.add_argument(
        "--baseline-version", type=int, metavar="N",
        help="Baseline is this saved version (default: the previous version, or no skill).",
    )
    shadow_parser.add_argument("--cases", type=Path, help="Directory of shadow case JSON files (default: evals/shadow).")
    accept_parser = commands.add_parser("accept", help="Validate and promote a proposal.")
    accept_parser.add_argument("proposal_id")
    reject_parser = commands.add_parser("reject", help="Reject a pending proposal.")
    reject_parser.add_argument("proposal_id")
    skills_parser = commands.add_parser("skills", help="List Harness-managed skills.")
    skills_parser.add_argument("--scope", choices=("project", "personal"))
    skills_parser.add_argument("--unused-days", type=int, metavar="N", help="List non-trusted skills not loaded in N days (suggestion only).")
    for action, help_text in (("archive", "Archive a managed skill."), ("restore", "Restore an archived skill.")):
        item = commands.add_parser(action, help=help_text)
        item.add_argument("name")
        item.add_argument("--scope", choices=("project", "personal"), default="project")
    for action, help_text in (
        ("trust", "Promote a probation skill to trusted."),
        ("unquarantine", "Restore a quarantined skill to probation."),
    ):
        item = commands.add_parser(action, help=help_text)
        item.add_argument("name")
        item.add_argument("--scope", choices=("project", "personal"), default="project")
    quarantine_parser = commands.add_parser("quarantine", help="Disable recall for a skill while preserving it.")
    quarantine_parser.add_argument("name")
    quarantine_parser.add_argument("--scope", choices=("project", "personal"), default="project")
    quarantine_parser.add_argument("--reason", required=True)
    rollback_parser = commands.add_parser("rollback", help="Restore a prior version of a managed skill.")
    rollback_parser.add_argument("name")
    rollback_parser.add_argument("--scope", choices=("project", "personal"), default="project")
    rollback_parser.add_argument("--version", type=int, help="Older version to restore; defaults to the previous version.")
    return parser


def _format_proposal(item: dict[str, Any], *, include_body: bool = False) -> str:
    status = item["status"]
    if status == "pending" and item.get("risk_level") == "sensitive":
        status = "candidate (approval required)"
    lines = [
        f"ID: {item['id']}",
        f"Status: {status}",
        f"Action: {item['action']}",
        f"Scope: {item['scope']}",
        f"Name: {item['name']}",
        f"Confidence: {item['confidence']:.2f}",
        f"Risk: {item.get('risk_level', 'low')}",
        f"Reason: {item['reason']}",
        f"Evidence: {len(item['evidence'])} captured events",
    ]
    if item.get("absorbs"):
        lines.insert(4, f"Absorbs: {', '.join(item['absorbs'])}")
    if include_body:
        lines += ["", item["skill_md"]]
    return "\n".join(lines)


def _record_skill_uses(storage: Storage, cwd: Path, session_id: str) -> None:
    raw = storage.latest_transcript_path(session_id, str(cwd))
    if raw:
        storage.record_skill_uses(session_id, read_skill_invocations(Path(raw)))


def _shadow(args: argparse.Namespace, config: Any, storage: Storage, cwd: Path) -> int:
    if args.proposal:
        proposal = storage.proposal(args.proposal)
        if not proposal or proposal["status"] != "pending":
            raise HarnessError(f"Pending proposal not found: {args.proposal}")
        name, scope, candidate = proposal["name"], proposal["scope"], proposal["skill_md"]
        baseline = None
        if proposal["action"] == "patch":
            baseline = inspect_skill(name, scope, cwd)[2]
        label = f"proposal {args.proposal}"
        baseline_label = "live skill" if baseline else "no skill"
    else:
        if not args.name:
            raise HarnessError("Give a skill name or --proposal ID.")
        name, scope = args.name, args.scope
        _, metadata, candidate = inspect_skill(name, scope, cwd)
        current = metadata.get("version", 1)
        wanted = args.baseline_version if args.baseline_version is not None else current - 1
        if args.baseline_version is not None and not 1 <= wanted < current:
            raise HarnessError("Baseline version must be an existing version older than the current one.")
        baseline = read_version(name, scope, cwd, wanted) if wanted >= 1 else None
        label = f"{scope} skill {name} v{current}"
        baseline_label = f"v{wanted}" if baseline else "no skill"
    directory = args.cases or Path.cwd() / "evals" / "shadow"
    if not directory.is_dir():
        raise HarnessError(f"Shadow case directory not found: {directory}")
    cases = load_task_cases(directory, name)
    if not cases:
        raise HarnessError(f"No shadow cases for {name} in {directory}. Cases need a 'task' and 'expect'.")
    others = [s for s in managed_skill_context(cwd) if (s["scope"], s["name"]) != (scope, name)]
    results = run_shadow(config, cases, candidate, baseline)
    print(f"Shadow evaluation: {label} vs {baseline_label} ({len(results)} case(s), one run each; results can vary)")
    for item in results:
        print(
            f"  [{item.outcome:11}] {item.name}  baseline={'pass' if item.baseline_passed else 'fail'} "
            f"candidate={'pass' if item.candidate_passed else 'fail'}"
        )
        for failure in item.failures:
            print(f"       - {failure}")
    conflicts = detect_conflicts(candidate, others)
    for conflict in conflicts:
        print(f"  [{conflict.kind}] {conflict.other}: {conflict.detail}")
    regressions = [item for item in results if item.outcome == "regression"]
    print(f"{len(regressions)} regression(s), {sum(i.outcome == 'improvement' for i in results)} improvement(s), "
          f"{len(conflicts)} possible conflict(s). The live skill was not changed.")
    return 1 if regressions else 0


def _handle(args: argparse.Namespace) -> int:
    config = load_config()
    storage = Storage(config.database)
    cwd = Path.cwd().resolve()

    if args.command == "hook":
        started = time.monotonic()
        debug_log(config, f"hook {args.event} received mode={config.mode}")
        def provide_context(ev: Any) -> str | None:
            context = session_start_context(config, Path(ev.cwd).resolve())
            debug_log(config, f"sessionStart index injection: {len(context) if context else 0} chars")
            return context

        event = handle_hook(storage, event_name=args.event, context_provider=provide_context)
        if not event.event_name:
            debug_log(config, f"hook {args.event} ignored: running inside a Harness reflector session")
            return 0
        event_cwd = Path(event.cwd).resolve() if event.cwd else cwd
        debug_log(
            config,
            f"hook {args.event} stored session={event.session_id or '-'} tool={event.tool_name or '-'} "
            f"outcome={event.outcome or '-'} cwd={event_cwd} in {time.monotonic() - started:.3f}s",
        )
        if event.event_name in {"agentStop", "sessionEnd"}:
            _record_skill_uses(storage, event_cwd, event.session_id)
        if event.event_name == "agentStop" and should_reflect_mid_session(
            storage, config, event_cwd, event.session_id
        ):
            spawn_background_reflection(event_cwd, event.session_id, config)
        if event.event_name == "sessionEnd" and config.mode == "auto":
            try:
                message = reflect_session(storage, config, event_cwd, event.session_id)
            except HarnessError as exc:
                log_reflection(config, f"failed session={event.session_id}: {exc}")
                raise
            if message:
                log_reflection(config, message)
                print(f"[copilot-harness] {message}", file=sys.stderr)
        return 0
    if args.command == "eval":
        directory = Path(args.cases) if args.cases else Path.cwd() / "evals" / "cases"
        if not directory.is_dir():
            raise HarnessError(f"Eval case directory not found: {directory}")
        results = run_eval(config, directory, args.only)
        for item in results:
            print(f"[{'PASS' if item.passed else 'FAIL'}] {item.name} (action: {item.action or 'n/a'})")
            for failure in item.failures:
                print(f"       - {failure}")
        passed = sum(1 for item in results if item.passed)
        print(f"{passed}/{len(results)} cases passed")
        return 0 if results and passed == len(results) else 1
    if args.command == "doctor":
        results = run_checks(config, cwd)
        for name, ok, detail in results:
            print(f"[{'ok' if ok else '!!'}] {name}: {detail}")
        return 0 if all(ok for _, ok, _ in results) else 1
    if args.command == "reflect-session":
        target = Path(args.cwd).resolve()
        try:
            message = reflect_session(storage, config, target, args.session_id)
        except HarnessError as exc:
            log_reflection(config, f"failed session={args.session_id}: {exc}")
            return 1
        if message:
            log_reflection(config, message)
        return 0
    if args.command == "config":
        if args.config_action == "init":
            config.home.mkdir(parents=True, exist_ok=True)
            if config.config_file.exists():
                raise HarnessError(f"Configuration already exists: {config.config_file}")
            config.config_file.write_text(default_config_text(), encoding="utf-8")
            print(f"Created {config.config_file}")
        else:
            print(f"Config: {config.config_file}")
            print(f"Mode: {config.mode}")
            print(f"Reflector command configured: {'yes' if config.reflect_command else 'no'}")
        return 0
    if args.command == "status":
        latest = storage.latest_session(str(cwd))
        print(f"Mode: {config.mode}")
        print(f"State: {config.database}")
        print(f"Latest captured session in this directory: {latest or 'none'}")
        print(f"Reflector command configured: {'yes' if config.reflect_command else 'no'}")
        print(f"Project skills: {len(list_skills(cwd, 'project'))}")
        print(f"Personal skills: {len(list_skills(cwd, 'personal'))}")
        pending = storage.proposals("pending")
        print(f"Candidates awaiting review: {len(pending)}")
        last = last_reflection_log(config)
        print(f"Last reflection: {last or 'none'}")
        print(f"Debug logging: {'on' if debug_enabled(config) else 'off'} ({config.home / DEBUG_LOG_NAME})")
        return 0
    if args.command == "logs":
        if not debug_enabled(config):
            print("Debug logging is off. Set [logging] debug = true in config.toml or COPILOT_HARNESS_DEBUG=1.")
        lines = tail_debug_log(config, args.lines)
        print("\n".join(lines) if lines else f"No debug log yet at {config.home / DEBUG_LOG_NAME}.")
        return 0
    if args.command == "learn":
        if config.mode == "auto":
            session_id = args.session_id or storage.latest_session(str(cwd))
            if not session_id:
                raise HarnessError("No captured Copilot session found for this directory.")
            message = reflect_session(storage, config, cwd, session_id, force=args.now)
            if message:
                log_reflection(config, message)
                print(message)
                return 0
            print("No eligible new lesson was found.")
            return 0
        result = learn(storage, config, cwd, args.session_id, force=args.now)
        if result is None:
            print("No reusable lesson was proposed.")
            return 0
        print("Proposal created:")
        print(_format_proposal({**result, "status": "pending"}))
        print(f"Review with `harness inspect {result['id']} --proposal`, then accept or reject.")
        return 0
    if args.command == "proposals":
        values = storage.proposals(None if args.all else "pending")
        if not values:
            print("No proposals.")
            return 0
        for item in values:
            print(_format_proposal(item))
            print()
        return 0
    if args.command == "inspect":
        if args.proposal:
            item = storage.proposal(args.name)
            if not item:
                raise HarnessError(f"Proposal not found: {args.name}")
            print(_format_proposal(item, include_body=True))
        else:
            directory, metadata, body = inspect_skill(args.name, args.scope, cwd)
            print(f"Path: {directory}")
            print(f"Status: {metadata.get('status', 'unknown')}")
            print(body)
            ledger = directory / ".ledger.jsonl"
            if ledger.is_symlink():
                raise HarnessError(f"Refusing to inspect a symlinked evidence ledger: {ledger}")
            if ledger.exists():
                print("Evidence ledger:")
                print(ledger.read_text(encoding="utf-8"))
        return 0
    if args.command == "accept":
        proposal = storage.proposal(args.proposal_id)
        if not proposal or proposal["status"] != "pending":
            raise HarnessError(f"Pending proposal not found: {args.proposal_id}")
        actual_risk = classify_risk(
            proposal["name"],
            proposal["description"],
            proposal["reason"],
            proposal["skill_md"],
        )
        proposal["approved_by"] = getpass.getuser()
        if proposal.get("risk_level") == "sensitive" or actual_risk == "sensitive":
            proposal["risk_level"] = "sensitive"
        capacity = (
            config.project_skill_capacity
            if proposal["scope"] == "project"
            else config.personal_skill_capacity
        )
        if proposal["action"] == "merge":
            path = merge_skills(proposal, Path(proposal["cwd"]))
        else:
            path = promote(proposal, Path(proposal["cwd"]), capacity)
        storage.set_proposal_status(args.proposal_id, "accepted")
        print(f"Promoted skill: {path}")
        return 0
    if args.command == "consolidate":
        scopes = [args.scope] if args.scope else ["project", "personal"]
        found = 0
        for scope in scopes:
            for item in consolidate(storage, config, cwd, scope, auto=config.mode == "auto"):
                found += 1
                merged = item["proposal"]
                state = "merged" if item["applied"] else "pending review"
                print(
                    f"{scope}: {', '.join(merged['absorbs'])} -> {merged['name']} "
                    f"({state}; proposal: {merged['id']})"
                )
        if not found:
            print("No same-scenario skills to merge.")
        return 0
    if args.command == "reject":
        storage.set_proposal_status(args.proposal_id, "rejected")
        print(f"Rejected proposal {args.proposal_id}")
        return 0
    if args.command == "skills":
        values = list_skills(cwd, args.scope)
        if not values:
            print("No Harness-managed skills.")
            return 0
        usage = storage.skill_usage()
        feedback = storage.skill_feedback(str(cwd))
        if args.unused_days is not None:
            idle = find_unused(values, usage, args.unused_days)
            if not idle:
                print(f"No non-trusted skills idle for {args.unused_days}+ days.")
            for item in idle:
                print(
                    f"{item['scope']:8} {item['status']:10} {item['name']}  idle {item['idle_days']}d "
                    f"since {item['basis']}  (harness archive {item['name']} --scope {item['scope']})"
                )
            return 0
        for item in values:
            uses, last = usage.get((item["scope"], item["name"]), (0, ""))
            seen = f"used {uses}x, last {last[:10]}" if uses else "never used"
            helpful, not_helpful = feedback.get((str(cwd), item["scope"], item["name"]), (0, 0))
            print(
                f"{item['scope']:8} {item['status']:10} {item['name']}  "
                f"[{seen}; feedback {helpful} helpful/{not_helpful} not helpful]  {item['path']}"
            )
        return 0
    if args.command == "feedback":
        _, metadata, _ = inspect_skill(args.name, args.scope, cwd)
        version = metadata.get("version", 1)
        if not isinstance(version, int) or version < 1:
            raise HarnessError(f"Invalid version metadata for {args.name}.")
        storage.record_skill_feedback(
            str(cwd),
            args.name,
            args.scope,
            version,
            args.rating,
            datetime.now(UTC).isoformat(),
        )
        print(f"Recorded {args.rating} feedback for {args.scope} skill {args.name} (version {version}).")
        return 0
    if args.command == "export":
        path = export_skill(args.name, args.scope, cwd, args.to)
        print(f"Exported trusted skill: {path}")
        return 0
    if args.command == "verify":
        digest = verify_export(args.directory)
        print(f"Content hash verified: {digest}")
        print("Note: the local hash manifest is unsigned and does not authenticate its publisher.")
        return 0
    if args.command == "audit":
        policy_path = args.policy or find_policy(Path.cwd(), args.directory.resolve().parent)
        policy = load_policy(policy_path) if policy_path else None
        if policy:
            print(f"Using policy {policy.path}")
        count, findings = audit_skills(args.directory, policy)
        for finding in findings:
            print(f"{finding.skill}: [{finding.check}] {finding.detail}")
        print(f"Audited {count} skill(s): {len(findings)} finding(s).")
        return 1 if findings else 0
    if args.command == "govern":
        path = set_governance(args.name, args.scope, cwd, args.owner, args.criticality, args.expires)
        print(f"Recorded governance for {args.scope} skill {args.name}: {path}")
        return 0
    if args.command == "bundle":
        print(f"Bundle written: {build_bundle(args.directory, args.out)}")
        return 0
    if args.command == "verify-attestation":
        policy_path = args.policy or find_policy(Path.cwd())
        policy = load_policy(policy_path) if policy_path else None
        repo = args.repo or (policy.trusted_repo if policy else None)
        workflow = args.workflow or (policy.trusted_workflow if policy else None)
        if not repo:
            raise HarnessError("Pass --repo or set trusted_repo in harness-policy.json.")
        print(f"Attestation verified: bundle built by {verify_attestation(args.bundle, repo, workflow)}")
        return 0
    if args.command == "sign":
        print(f"Signed: {sign_export(args.directory, args.key)}")
        return 0
    if args.command == "verify-signature":
        print(f"Signature verified for signer: {verify_signature(args.directory, args.allowed_signers)}")
        return 0
    if args.command in {"share", "import"}:
        if args.command == "share":
            proposal = share_proposal(args.name, args.source, args.target, cwd)
        else:
            policy_path = args.policy or find_policy(cwd)
            if policy_path:
                policy = load_policy(policy_path)
                verify_export(args.directory)
                enforce_policy(args.directory, policy)
            proposal = import_proposal(args.directory, args.scope, cwd)
        storage.add_proposal(proposal, str(cwd), datetime.now(UTC).isoformat())
        print("Proposal created (nothing installed yet):")
        print(_format_proposal({**proposal, "status": "pending"}))
        print(f"Review with `harness inspect {proposal['id']} --proposal`, then `harness accept {proposal['id']}`.")
        return 0
    if args.command == "shared-init":
        for path in scaffold_shared_repo(args.directory):
            print(f"Created {path}")
        print("Edit .github/CODEOWNERS and enable branch protection with required code-owner review.")
        return 0
    if args.command == "shadow":
        return _shadow(args, config, storage, cwd)
    if args.command in {"archive", "restore"}:
        path = move_skill(args.name, args.scope, cwd, archive=args.command == "archive")
        print(f"{'Archived' if args.command == 'archive' else 'Restored'}: {path}")
        return 0
    if args.command == "trust":
        path = trust_skill(args.name, args.scope, cwd)
        print(f"Trusted: {path}")
        return 0
    if args.command == "quarantine":
        path = quarantine_skill(args.name, args.scope, cwd, args.reason)
        print(f"Quarantined: {path}")
        return 0
    if args.command == "unquarantine":
        path = restore_quarantined_skill(args.name, args.scope, cwd)
        print(f"Restored to probation: {path}")
        return 0
    if args.command == "rollback":
        path = rollback_skill(args.name, args.scope, cwd, args.version)
        print(f"Rolled back: {path}")
        return 0
    raise HarnessError(f"Unsupported command: {args.command}")


def _log_failure(args: argparse.Namespace, exc: BaseException) -> None:
    try:
        config = load_config()
    except HarnessError:
        config = None
    debug_exception(config, f"command {getattr(args, 'command', '?')}", exc)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return _handle(args)
    except HarnessError as exc:
        _log_failure(args, exc)
        print(f"harness: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        _log_failure(args, exc)
        print(f"harness: {exc}", file=sys.stderr)
        return 1
