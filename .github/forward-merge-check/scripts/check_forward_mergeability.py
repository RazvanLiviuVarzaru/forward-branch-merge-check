#!/usr/bin/env python3

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from enum import Enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


DEFAULT_BRANCHES = ["10.6", "10.11", "11.4", "11.8", "12.3", "main"]
DEFAULT_CONFIG_FILE = Path(".github/forward-merge-check/forward-merge-chain.yml")
LEGACY_BRANCH_FILE = Path(".github/forward-merge-branches.txt")


@dataclass
class CommitInfo:
    sha: str
    author: str
    subject: str


@dataclass
class MergeResult:
    source_label: str
    source_ref: str
    target: str
    status: str
    message: str
    conflicted_files: list[str] = field(default_factory=list)
    first_conflicting_commit: Optional[CommitInfo] = None
    candidate_commits: list[CommitInfo] = field(default_factory=list)


class NotificationReason(str, Enum):
    FIRST_RUN = "first_run"
    BROKEN = "broken"
    RESOLVED = "resolved"
    HEALTH_CHANGED = "health_changed"
    CHAIN_CHANGED = "chain_changed"


def run(
    args: list[str],
    cwd: Optional[Path] = None,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        args,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )

    if check and proc.returncode != 0:
        raise RuntimeError(
            f"Command failed: {' '.join(args)}\n"
            f"exit code: {proc.returncode}\n"
            f"stdout:\n{proc.stdout or ''}\n"
            f"stderr:\n{proc.stderr or ''}"
        )

    return proc


def git(repo: Path, args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return run(["git", *args], cwd=repo, check=check)


def parse_scalar(value: str) -> object:
    value = value.strip()

    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if value in {"null", "Null", "~"}:
        return None
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    return value


def read_simple_yaml(path: Path) -> dict:
    """
    Parse the tiny YAML subset used by .github/forward-merge-check/forward-merge-chain.yml.

    Supported forms:
      key: value
      parent:
        child: value
      list:
        - value
    """
    root: dict = {}
    stack: list[tuple[int, dict | list]] = [(-1, root)]
    pending_key: tuple[int, dict, str] | None = None

    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue

        indent = len(raw_line) - len(raw_line.lstrip(" "))
        stripped = raw_line.strip()

        while stack and indent <= stack[-1][0]:
            stack.pop()

        if pending_key and indent > pending_key[0]:
            parent_indent, parent, key = pending_key
            container: dict | list = [] if stripped.startswith("- ") else {}
            parent[key] = container
            stack.append((parent_indent, container))
            pending_key = None

        parent = stack[-1][1]

        if stripped.startswith("- "):
            if not isinstance(parent, list):
                raise ValueError(f"{path}:{line_number}: list item outside a list")
            parent.append(parse_scalar(stripped[2:]))
            continue

        if ":" not in stripped:
            raise ValueError(f"{path}:{line_number}: expected 'key: value'")

        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()

        if not isinstance(parent, dict):
            raise ValueError(f"{path}:{line_number}: mapping item outside a mapping")

        if value:
            parent[key] = parse_scalar(value)
            pending_key = None
        else:
            pending_key = (indent, parent, key)

    return root


def read_branch_config(path: Path) -> list[str]:
    branches: list[str] = []

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        branches.append(line)

    if len(branches) < 2:
        raise ValueError(f"{path} must contain at least two branches.")

    if len(set(branches)) != len(branches):
        raise ValueError(f"{path} contains duplicate branches.")

    return branches


def load_config(path: Path) -> dict:
    if path.exists():
        cfg = read_simple_yaml(path)
        branches = cfg.get("branches")
        if not isinstance(branches, list) or not all(isinstance(branch, str) for branch in branches):
            raise ValueError(f"{path} must define a 'branches' list.")

        base_branch = cfg.get("base_branch") or branches[0]
        if not isinstance(base_branch, str):
            raise ValueError(f"{path} base_branch must be a string.")

        return {
            "branches": branches,
            "base_branch": base_branch,
            "notifications": cfg.get("notifications") or {},
        }

    if LEGACY_BRANCH_FILE.exists():
        branches = read_branch_config(LEGACY_BRANCH_FILE)
        return {
            "branches": branches,
            "base_branch": branches[0],
            "notifications": {},
        }

    return {
        "branches": DEFAULT_BRANCHES,
        "base_branch": DEFAULT_BRANCHES[0],
        "notifications": {},
    }


def load_branches(args: argparse.Namespace) -> list[str]:
    if args.branches:
        branches = args.branches
    elif args.branch_file and args.branch_file.exists():
        branches = read_branch_config(args.branch_file)
    elif os.environ.get("FORWARD_MERGE_BRANCHES"):
        branches = os.environ["FORWARD_MERGE_BRANCHES"].split()
    else:
        branches = load_config(args.config_file)["branches"]

    if len(branches) < 2:
        raise ValueError("At least two branches are required.")

    if len(set(branches)) != len(branches):
        raise ValueError("Branch list contains duplicates.")

    return branches


def load_base_branch(args: argparse.Namespace, branches: list[str]) -> str:
    if args.base_branch:
        return args.base_branch

    cfg = load_config(args.config_file)
    base_branch = cfg.get("base_branch") or branches[0]
    if not isinstance(base_branch, str):
        raise ValueError("Configured base_branch must be a string.")

    return base_branch


def remote_ref(branch: str) -> str:
    return f"refs/remotes/origin/{branch}"


def ensure_ref(repo: Path, ref: str) -> None:
    git(repo, ["rev-parse", "--verify", "--quiet", ref])


def has_ref(repo: Path, ref: str) -> bool:
    proc = git(repo, ["rev-parse", "--verify", "--quiet", ref], check=False)
    return proc.returncode == 0


def missing_branch_refs(repo: Path, branches: list[str]) -> list[str]:
    return [branch for branch in branches if not has_ref(repo, remote_ref(branch))]


def format_missing_branch_refs_error(repo: Path, branches: list[str], missing: list[str]) -> str:
    configured = "\n".join(f"  - {branch}" for branch in branches)
    fetch_lines = " \\\n  ".join(
        f"+refs/heads/{branch}:refs/remotes/origin/{branch}" for branch in missing
    )

    return (
        "The target repository is missing configured branch refs.\n\n"
        f"Target repository: {repo}\n\n"
        "Configured branch chain:\n"
        f"{configured}\n\n"
        "Missing remote-tracking refs:\n"
        + "\n".join(f"  - {remote_ref(branch)}" for branch in missing)
        + "\n\n"
        "Fetch the missing refs into the target repository:\n\n"
        f"git -C {repo} fetch origin \\\n"
        f"  {fetch_lines}\n\n"
        "If one of these branches no longer exists, update the configured chain "
        "instead of fetching it."
    )


def ensure_branch_refs(repo: Path, branches: list[str]) -> None:
    missing = missing_branch_refs(repo, branches)
    if missing:
        raise ValueError(format_missing_branch_refs_error(repo, branches, missing))


def is_ancestor(repo: Path, maybe_ancestor: str, descendant: str) -> bool:
    proc = git(
        repo,
        ["merge-base", "--is-ancestor", maybe_ancestor, descendant],
        check=False,
    )
    return proc.returncode == 0


def get_commit_info(repo: Path, sha: str) -> CommitInfo:
    fmt = "%H%x00%an <%ae>%x00%s"
    proc = git(repo, ["show", "-s", f"--format={fmt}", sha])
    commit_sha, author, subject = proc.stdout.rstrip("\n").split("\x00", 2)
    return CommitInfo(sha=commit_sha, author=author, subject=subject)


def branch_head(repo: Path, branch: str) -> str:
    ensure_ref(repo, remote_ref(branch))
    return git(repo, ["rev-parse", remote_ref(branch)]).stdout.strip()


def add_worktree(repo: Path, ref: str, scratch_root: Path) -> Path:
    worktree = Path(tempfile.mkdtemp(prefix="merge-check-", dir=scratch_root))
    shutil.rmtree(worktree)
    git(repo, ["worktree", "add", "--detach", "--quiet", str(worktree), ref])
    git(worktree, ["config", "user.name", "Forward Mergeability Checker"])
    git(worktree, ["config", "user.email", "forward-mergeability@example.invalid"])
    return worktree


def remove_worktree(repo: Path, worktree: Path) -> None:
    git(repo, ["worktree", "remove", "--force", str(worktree)], check=False)


def get_conflicted_files(worktree: Path) -> list[str]:
    proc = git(worktree, ["diff", "--name-only", "--diff-filter=U"])
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def source_commits(repo: Path, target_ref: str, source_ref: str) -> list[str]:
    proc = git(
        repo,
        [
            "rev-list",
            "--reverse",
            "--no-merges",
            f"{target_ref}..{source_ref}",
        ],
    )
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def try_merge(repo: Path, base_ref: str, source_ref: str, scratch_root: Path) -> tuple[bool, list[str], Optional[str]]:
    worktree = add_worktree(repo, base_ref, scratch_root)

    try:
        proc = git(
            worktree,
            [
                "merge",
                "--no-ff",
                "-m",
                f"Local test merge: {source_ref} into {base_ref}",
                source_ref,
            ],
            check=False,
        )

        if proc.returncode == 0:
            sha = git(worktree, ["rev-parse", "HEAD"]).stdout.strip()
            return True, [], sha

        return False, get_conflicted_files(worktree), None

    finally:
        git(worktree, ["merge", "--abort"], check=False)
        remove_worktree(repo, worktree)


def find_first_conflicting_commit(
    repo: Path,
    source_ref: str,
    target: str,
    scratch_root: Path,
) -> Optional[CommitInfo]:
    """
    Approximation.

    Each source-side non-merge commit is tried independently against the target.
    Git conflicts are produced by both branches together, so this is not a
    perfect root cause. It is a practical answer to "which introduced commit
    should I inspect first?" for scheduled chain-health runs too, not only PRs.
    """
    target_ref = remote_ref(target)

    for sha in source_commits(repo, target_ref, source_ref):
        ok, _files, _merge_sha = try_merge(repo, target_ref, sha, scratch_root)
        if not ok:
            return get_commit_info(repo, sha)

    return None


def candidate_commits_for_conflicted_files(
    repo: Path,
    source_ref: str,
    target: str,
    files: list[str],
    limit_per_file: int = 20,
) -> list[CommitInfo]:
    target_ref = remote_ref(target)
    seen: set[str] = set()
    candidates: list[CommitInfo] = []

    for file_path in files:
        proc = git(
            repo,
            [
                "log",
                "--no-merges",
                "--format=%H",
                f"{target_ref}..{source_ref}",
                "--",
                file_path,
            ],
            check=False,
        )

        for sha in [line.strip() for line in proc.stdout.splitlines() if line.strip()]:
            if sha in seen:
                continue

            seen.add(sha)
            candidates.append(get_commit_info(repo, sha))

            if len(candidates) >= limit_per_file:
                break

    return candidates


def check_merge(
    repo: Path,
    source_label: str,
    source_ref: str,
    target: str,
    scratch_root: Path,
) -> MergeResult:
    target_ref = remote_ref(target)
    ensure_ref(repo, target_ref)
    ensure_ref(repo, source_ref)

    if is_ancestor(repo, source_ref, target_ref):
        return MergeResult(
            source_label=source_label,
            source_ref=source_ref,
            target=target,
            status="nothing_to_merge",
            message=f"{source_label} is already contained in {target}.",
        )

    ok, conflicted_files, merge_sha = try_merge(repo, target_ref, source_ref, scratch_root)

    if ok:
        assert merge_sha is not None
        return MergeResult(
            source_label=source_label,
            source_ref=merge_sha,
            target=target,
            status="merge_ok",
            message=f"{source_label} merges cleanly into {target}.",
        )

    first_conflicting_commit = find_first_conflicting_commit(
        repo=repo,
        source_ref=source_ref,
        target=target,
        scratch_root=scratch_root,
    )
    candidates = candidate_commits_for_conflicted_files(
        repo=repo,
        source_ref=source_ref,
        target=target,
        files=conflicted_files,
    )

    return MergeResult(
        source_label=source_label,
        source_ref=source_ref,
        target=target,
        status="conflict",
        message=f"{source_label} cannot be cleanly merged into {target}.",
        conflicted_files=conflicted_files,
        first_conflicting_commit=first_conflicting_commit,
        candidate_commits=candidates,
    )


def branch_targets_after(branches: list[str], base_branch: str) -> list[str]:
    if base_branch not in branches:
        raise ValueError(
            f"Base branch {base_branch!r} is not in the configured branch chain: "
            + ", ".join(branches)
        )

    return branches[branches.index(base_branch) + 1 :]


def commit_to_dict(commit: Optional[CommitInfo]) -> Optional[dict]:
    if commit is None:
        return None
    return {
        "sha": commit.sha,
        "author": commit.author,
        "subject": commit.subject,
    }


def result_to_dict(result: MergeResult) -> dict:
    return {
        "source_label": result.source_label,
        "source_ref": result.source_ref,
        "target": result.target,
        "status": result.status,
        "message": result.message,
        "conflicted_files": result.conflicted_files,
        "first_conflicting_commit": commit_to_dict(result.first_conflicting_commit),
        "candidate_commits": [commit_to_dict(commit) for commit in result.candidate_commits],
    }


def stable_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def fingerprint(value: object) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def current_state(repo: Path, branches: list[str], base_branch: str, results: list[MergeResult]) -> dict:
    branch_heads = {branch: branch_head(repo, branch) for branch in branches}
    blocked = next((result for result in results if result.status == "conflict"), None)
    status = "broken" if blocked else "healthy"

    config_payload = {
        "branches": branches,
        "base_branch": base_branch,
    }
    chain_payload = {
        **config_payload,
        "branch_heads": branch_heads,
    }
    health_payload = {
        "status": status,
        "blocked_edge": None if blocked is None else [blocked.source_label, blocked.target],
        "conflicted_files": [] if blocked is None else blocked.conflicted_files,
        "first_conflicting_commit": None
        if blocked is None or blocked.first_conflicting_commit is None
        else blocked.first_conflicting_commit.sha,
        "results": [
            {
                "target": result.target,
                "status": result.status,
                "conflicted_files": result.conflicted_files,
            }
            for result in results
        ],
    }

    return {
        "version": 1,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "base_branch": base_branch,
        "branches": branches,
        "branch_heads": branch_heads,
        "config_fingerprint": fingerprint(config_payload),
        "chain_fingerprint": fingerprint(chain_payload),
        "health_fingerprint": fingerprint(health_payload),
        "results": [result_to_dict(result) for result in results],
    }


def load_previous_state(raw_state: str | None) -> Optional[dict]:
    if not raw_state:
        return None

    try:
        parsed = json.loads(raw_state)
    except json.JSONDecodeError:
        return None

    return parsed if isinstance(parsed, dict) else None


def load_previous_state_file(path: Optional[Path]) -> Optional[dict]:
    if path is None or not path.exists():
        return None

    return load_previous_state(path.read_text(encoding="utf-8"))


def notification_reasons(previous: Optional[dict], state: dict) -> list[NotificationReason]:
    if previous is None:
        return [NotificationReason.FIRST_RUN] if state["status"] == "broken" else []

    reasons: list[NotificationReason] = []

    if previous.get("config_fingerprint") != state.get("config_fingerprint"):
        reasons.append(NotificationReason.CHAIN_CHANGED)

    old_status = previous.get("status")
    new_status = state.get("status")

    if old_status != "broken" and new_status == "broken":
        reasons.append(NotificationReason.BROKEN)
    elif old_status == "broken" and new_status != "broken":
        reasons.append(NotificationReason.RESOLVED)
    elif old_status == "broken" and new_status == "broken":
        if previous.get("health_fingerprint") != state.get("health_fingerprint"):
            reasons.append(NotificationReason.HEALTH_CHANGED)

    if (
        previous.get("chain_fingerprint") != state.get("chain_fingerprint")
        and NotificationReason.CHAIN_CHANGED not in reasons
        and new_status == "broken"
    ):
        reasons.append(NotificationReason.CHAIN_CHANGED)

    return reasons


def github_repository_label() -> str:
    return os.environ.get("GITHUB_REPOSITORY", "unknown repository")


def github_repository_url() -> Optional[str]:
    repository = os.environ.get("GITHUB_REPOSITORY")
    if not repository:
        return None

    server_url = os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    return f"{server_url}/{repository}"


def github_action_run_url() -> Optional[str]:
    repository_url = github_repository_url()
    run_id = os.environ.get("GITHUB_RUN_ID")
    if not repository_url or not run_id:
        return None

    return f"{repository_url}/actions/runs/{run_id}"


def github_commit_url(sha: str) -> Optional[str]:
    repository_url = github_repository_url()
    if not repository_url:
        return None

    return f"{repository_url}/commit/{sha}"


def slack_link(url: Optional[str], label: str) -> str:
    return f"<{url}|{label}>" if url else label


def humanize_notification_reasons(reasons: list[NotificationReason]) -> str:
    labels = {
        NotificationReason.FIRST_RUN: "first observed broken state",
        NotificationReason.BROKEN: "chain became blocked",
        NotificationReason.RESOLVED: "chain recovered",
        NotificationReason.HEALTH_CHANGED: "blocked result changed",
        NotificationReason.CHAIN_CHANGED: "configured chain changed",
    }
    return ", ".join(labels.get(reason, reason.value) for reason in reasons)


def format_notification(state: dict, reasons: list[NotificationReason]) -> str:
    headline = "Forward merge chain is healthy"
    if state["status"] == "broken":
        headline = "Forward merge chain is blocked"

    lines = [
        f"*Repository:* `{github_repository_label()}`",
        f"*{headline}*",
    ]

    if reasons:
        lines.append(f"*Reason:* {humanize_notification_reasons(reasons)}")

    blocked = next((result for result in state["results"] if result["status"] == "conflict"), None)
    if blocked:
        lines.append(f"*Blocked edge:* `{blocked['source_label']}` -> `{blocked['target']}`")

    lines.append(f"*Chain:* {' -> '.join(f'`{branch}`' for branch in state['branches'])}")

    action_run_url = github_action_run_url()
    if action_run_url:
        lines.append(f"*GitHub Actions run:* {slack_link(action_run_url, 'open run')}")

    if blocked:
        commit = blocked.get("first_conflicting_commit")
        if commit:
            short_sha = commit["sha"][:12]
            commit_label = slack_link(github_commit_url(commit["sha"]), short_sha)
            lines.append(
                "*First likely source-side commit:* "
                f"{commit_label} - {commit['subject']}"
            )
            lines.append(f"*Author:* {commit['author']}")

        if blocked["conflicted_files"]:
            lines.append(f"*Conflicted files ({len(blocked['conflicted_files'])}):*")
            lines.extend(f"- `{path}`" for path in blocked["conflicted_files"])

    return "\n".join(lines)


def compact_state(state: dict) -> dict:
    return {
        "version": state["version"],
        "checked_at": state["checked_at"],
        "status": state["status"],
        "base_branch": state["base_branch"],
        "branches": state["branches"],
        "branch_heads": state["branch_heads"],
        "config_fingerprint": state["config_fingerprint"],
        "chain_fingerprint": state["chain_fingerprint"],
        "health_fingerprint": state["health_fingerprint"],
        "results": [
            {
                "source_label": result["source_label"],
                "target": result["target"],
                "status": result["status"],
                "conflicted_files": result["conflicted_files"],
                "first_conflicting_commit": None
                if result["first_conflicting_commit"] is None
                else {
                    "sha": result["first_conflicting_commit"]["sha"],
                    "subject": result["first_conflicting_commit"]["subject"],
                },
            }
            for result in state["results"]
        ],
    }


def write_outputs(args: argparse.Namespace, state: dict, reasons: list[NotificationReason]) -> None:
    if args.state_output:
        args.state_output.write_text(
            json.dumps(compact_state(state), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    notification = {
        "notify": bool(reasons),
        "reasons": [reason.value for reason in reasons],
        "status": state["status"],
        "text": format_notification(state, reasons) if reasons else "",
        "state": state,
    }

    if args.notification_output:
        args.notification_output.write_text(
            json.dumps(notification, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write(f"status={state['status']}\n")
            output.write(f"should_notify={'true' if reasons else 'false'}\n")
            output.write(f"health_fingerprint={state['health_fingerprint']}\n")
            output.write(f"chain_fingerprint={state['chain_fingerprint']}\n")


def print_result(result: MergeResult) -> None:
    print()
    print(f"Merge check: {result.source_label} -> {result.target}")
    print("-" * 72)
    print(f"Status: {result.status}")
    print(result.message)

    if result.conflicted_files:
        print()
        print("Conflicted files:")
        for path in result.conflicted_files:
            print(f"  - {path}")

    if result.first_conflicting_commit:
        c = result.first_conflicting_commit
        print()
        print("First likely source-side commit that introduces the conflict:")
        print(f"  - {c.sha}")
        print(f"    Author:  {c.author}")
        print(f"    Subject: {c.subject}")

    if result.candidate_commits:
        print()
        print("Candidate source-side commits touching conflicted files:")
        for c in result.candidate_commits:
            print(f"  - {c.sha}")
            print(f"    Author:  {c.author}")
            print(f"    Subject: {c.subject}")


def print_summary(results: list[MergeResult]) -> None:
    print()
    print("=" * 72)
    print("Summary")
    print("=" * 72)

    if not results:
        print("No forward branches to check.")
        print("Final result: forward merge chain is clean.")
        return

    for result in results:
        print(f"{result.source_label} -> {result.target}: {result.status}")

    print()

    if any(result.status == "conflict" for result in results):
        print("Final result: forward merge chain is blocked by conflicts.")
    else:
        print("Final result: forward merge chain is clean or already merged.")


def next_source_after_result(result: MergeResult) -> tuple[str, str]:
    if result.status == "merge_ok":
        return f"test merge through {result.target}", result.source_ref

    return result.target, remote_ref(result.target)


def first_downstream_broken_target_index(
    branches: list[str],
    pr_base_branch: str,
    baseline_results: list[MergeResult],
) -> Optional[int]:
    pr_base_index = branches.index(pr_base_branch)

    for result in baseline_results:
        if result.status != "conflict":
            continue

        target_index = branches.index(result.target)
        if target_index > pr_base_index:
            return target_index

    return None


def run_pr_mode(
    repo: Path,
    branches: list[str],
    base_branch: str,
    pr_ref: str,
    scratch_root: Path,
) -> list[MergeResult]:
    results: list[MergeResult] = []

    if base_branch not in branches:
        print()
        print(f"PR base branch {base_branch!r} is not in the configured forward-merge chain.")
        print("Skipping PR forward-merge check.")
        return results

    print()
    print(f"Checking baseline chain health from {base_branch} before applying PR changes.")
    baseline_results = run_chain_health_mode(repo, branches, base_branch, scratch_root)
    broken_target_index = first_downstream_broken_target_index(
        branches,
        base_branch,
        baseline_results,
    )
    base_index = branches.index(base_branch)
    stop_index = broken_target_index if broken_target_index is not None else len(branches)
    targets = branches[base_index + 1 : stop_index]

    if broken_target_index is not None:
        print()
        print(
            "Stopping PR forward checks before "
            f"{branches[broken_target_index]} because the baseline chain is already broken there."
        )

    if not targets:
        print()
        print("No PR forward-merge targets remain before the next pre-existing chain break.")
        return results

    source_label = "PR head"
    source_ref = pr_ref

    for target in targets:
        result = check_merge(repo, source_label, source_ref, target, scratch_root)
        results.append(result)
        print_result(result)

        if result.status == "conflict":
            break

        source_label, source_ref = next_source_after_result(result)

    return results


def run_chain_health_mode(repo: Path, branches: list[str], base_branch: str, scratch_root: Path) -> list[MergeResult]:
    results: list[MergeResult] = []
    source_label = base_branch
    source_ref = remote_ref(base_branch)

    for target in branch_targets_after(branches, base_branch):
        result = check_merge(repo, source_label, source_ref, target, scratch_root)
        results.append(result)
        print_result(result)

        source_label, source_ref = next_source_after_result(result)

    return results


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check whether changes can be forward-merged through a branch chain."
    )
    parser.add_argument("--mode", choices=["pr", "chain-health"])
    parser.add_argument("--base-branch")
    parser.add_argument("--pr-ref", help="PR head ref. Required for --mode pr.")
    parser.add_argument(
        "--config-file",
        type=Path,
        default=DEFAULT_CONFIG_FILE,
        help="Forward-merge chain YAML config.",
    )
    parser.add_argument(
        "--branch-file",
        type=Path,
        default=None,
        help="Legacy ordered branch-chain file, one branch per line.",
    )
    parser.add_argument(
        "--branches",
        nargs="+",
        help="Ordered branch chain. Overrides --branch-file and FORWARD_MERGE_BRANCHES.",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path.cwd(),
        help="Repository path. Defaults to the current directory.",
    )
    parser.add_argument(
        "--keep-worktrees",
        action="store_true",
        help="Keep temporary worktree root for debugging.",
    )
    parser.add_argument(
        "--state-input",
        default="",
        help="Previous state JSON.",
    )
    parser.add_argument("--state-input-file", type=Path, help="Read previous state JSON from this file.")
    parser.add_argument("--state-output", type=Path, help="Write current state JSON here.")
    parser.add_argument("--notification-output", type=Path, help="Write notification decision JSON here.")
    parser.add_argument("--github-output", type=Path, default=Path(os.environ["GITHUB_OUTPUT"]) if os.environ.get("GITHUB_OUTPUT") else None)
    parser.add_argument("--print-branches", action="store_true", help="Print configured branches and exit.")
    parser.add_argument("--print-base-branch", action="store_true", help="Print configured base branch and exit.")

    args = parser.parse_args()

    scratch_root = Path(tempfile.mkdtemp(prefix="forward-mergeability-"))

    try:
        repo = args.repo.resolve()
        branches = load_branches(args)
        base_branch = load_base_branch(args, branches)

        if args.print_branches:
            print("\n".join(branches))
            return 0

        if args.print_base_branch:
            print(base_branch)
            return 0

        if not args.mode:
            raise ValueError("--mode is required unless using --print-branches or --print-base-branch.")

        print("Configured branch chain:")
        for branch in branches:
            print(f"  - {branch}")

        print()
        print(f"Mode: {args.mode}")
        print(f"Base branch: {base_branch}")

        ensure_branch_refs(repo, branches)

        if args.mode == "pr":
            if not args.pr_ref:
                raise ValueError("--pr-ref is required in PR mode.")
            results = run_pr_mode(
                repo,
                branches,
                base_branch,
                args.pr_ref,
                scratch_root,
            )
        else:
            results = run_chain_health_mode(repo, branches, base_branch, scratch_root)

        print_summary(results)

        if args.mode == "chain-health":
            state = current_state(repo, branches, base_branch, results)
            previous_state = load_previous_state_file(args.state_input_file)
            if previous_state is None:
                previous_state = load_previous_state(args.state_input)
            reasons = notification_reasons(previous_state, state)
            write_outputs(args, state, reasons)

            if reasons:
                print()
                print("Notification decision: send")
                print("Reasons: " + ", ".join(reason.value for reason in reasons))
            else:
                print()
                print("Notification decision: suppress; no tracked state change.")

        return 1 if any(result.status == "conflict" for result in results) else 0

    except Exception as exc:
        print()
        print("ERROR")
        print("-" * 72)
        print(str(exc))
        return 2

    finally:
        if args.keep_worktrees:
            print()
            print(f"Temporary worktree root kept at: {scratch_root}")
        else:
            shutil.rmtree(scratch_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
