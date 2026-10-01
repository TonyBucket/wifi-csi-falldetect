#!/usr/bin/env python3
"""
scripts/code_sync.py

Safe, bi-directional code synchronization tool using Git.
Guarantees:
- Never uses --force or reset --hard.
- Never overwrites a dirty working directory during reconcile.
- Cleanly detects conflicts and guides manual resolution.
- Handles offline situations gracefully.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def run_git(
    args: List[str],
    cwd: Optional[Path] = None,
    timeout: Optional[float] = None,
) -> Tuple[int, str, str]:
    """Execute a git command using subprocess argv (no shell)."""
    try:
        proc = subprocess.run(
            ["git"] + args,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "Command timed out"
    except Exception as exc:
        return 1, "", str(exc)


def is_repo_root(cwd: Optional[Path] = None) -> bool:
    code, out, _ = run_git(["rev-parse", "--is-inside-work-tree"], cwd=cwd)
    return code == 0 and out == "true"


def get_current_branch(cwd: Optional[Path] = None) -> str:
    code, out, _ = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    if code != 0:
        return "HEAD"
    return out


def get_dirty_files(cwd: Optional[Path] = None) -> List[str]:
    code, out, _ = run_git(["status", "--porcelain"], cwd=cwd)
    if code != 0 or not out:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def check_remote_reachable(
    remote: str = "origin",
    cwd: Optional[Path] = None,
    timeout: float = 5.0,
) -> bool:
    code, _, _ = run_git(["ls-remote", remote], cwd=cwd, timeout=timeout)
    return code == 0


def get_upstream_divergence(
    branch: str,
    remote: str = "origin",
    cwd: Optional[Path] = None,
) -> Tuple[int, int]:
    """Returns (ahead, behind) commit counts against remote tracking branch."""
    tracking = f"{remote}/{branch}"
    # Check if tracking ref exists
    code, _, _ = run_git(["rev-parse", "--verify", tracking], cwd=cwd)
    if code != 0:
        return 0, 0
    code, out, _ = run_git(
        ["rev-list", "--left-right", "--count", f"{branch}...{tracking}"],
        cwd=cwd,
    )
    if code != 0 or not out:
        return 0, 0
    parts = out.split()
    if len(parts) == 2:
        return int(parts[0]), int(parts[1])
    return 0, 0


def inspect_status(
    remote: str = "origin",
    cwd: Optional[Path] = None,
    check_network: bool = True,
) -> Dict[str, Any]:
    if not is_repo_root(cwd):
        return {"error": "Not a git repository"}

    branch = get_current_branch(cwd)
    dirty_files = get_dirty_files(cwd)
    is_clean = len(dirty_files) == 0

    remote_ok = check_remote_reachable(remote=remote, cwd=cwd) if check_network else False
    ahead, behind = get_upstream_divergence(branch, remote=remote, cwd=cwd)

    # Determine state
    if not is_clean:
        state = "DIRTY"
    elif not remote_ok and check_network:
        state = "OFFLINE"
    elif ahead > 0 and behind > 0:
        state = "DIVERGED"
    elif ahead > 0:
        state = "AHEAD"
    elif behind > 0:
        state = "BEHIND"
    else:
        state = "CLEAN"

    return {
        "branch": branch,
        "is_clean": is_clean,
        "dirty_files": dirty_files,
        "remote": remote,
        "remote_reachable": remote_ok,
        "ahead": ahead,
        "behind": behind,
        "state": state,
    }


def cmd_status(args: argparse.Namespace) -> int:
    cwd = Path(args.repo) if args.repo else Path.cwd()
    st = inspect_status(remote=args.remote, cwd=cwd, check_network=not args.no_network)
    if "error" in st:
        print(f"Error: {st['error']}", file=sys.stderr)
        return 1

    print(f"Branch: {st['branch']}")
    print(f"Working tree: {'Clean' if st['is_clean'] else 'Dirty (' + str(len(st['dirty_files'])) + ' changed files)'}")
    if not st["is_clean"]:
        for f in st["dirty_files"][:10]:
            print(f"  {f}")
        if len(st["dirty_files"]) > 10:
            print(f"  ... and {len(st['dirty_files']) - 10} more")

    print(f"Remote ({st['remote']}): {'Reachable' if st['remote_reachable'] else ('Offline / Unreachable' if not args.no_network else 'Skipped')}")
    print(f"Commits: {st['ahead']} ahead, {st['behind']} behind remote")
    print(f"Overall State: {st['state']}")
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    cwd = Path(args.repo) if args.repo else Path.cwd()
    if not is_repo_root(cwd):
        print("Error: Not inside a git repository", file=sys.stderr)
        return 1

    dirty_files = get_dirty_files(cwd)
    if dirty_files:
        if args.auto_commit or args.message:
            msg = args.message or "Auto-commit before sync publish"
            code, _, err = run_git(["add", "."], cwd=cwd)
            if code != 0:
                print(f"Error staging files: {err}", file=sys.stderr)
                return 1
            code, _, err = run_git(["commit", "-m", msg], cwd=cwd)
            if code != 0:
                print(f"Error committing: {err}", file=sys.stderr)
                return 1
            print(f"Committed changes: '{msg}'")
        else:
            print("Error: Working tree is dirty. Stash/commit your changes or pass --auto-commit.", file=sys.stderr)
            for f in dirty_files[:5]:
                print(f"  {f}", file=sys.stderr)
            return 1

    branch = get_current_branch(cwd)
    target_branch = args.branch or branch
    remote = args.remote

    # Check remote connectivity
    if not check_remote_reachable(remote=remote, cwd=cwd):
        print(f"Error: Remote '{remote}' is unreachable (offline). Local commits preserved.", file=sys.stderr)
        return 2

    # Push without force
    print(f"Pushing branch '{branch}' to {remote}/{target_branch}...")
    code, out, err = run_git(["push", remote, f"{branch}:{target_branch}"], cwd=cwd)
    if code != 0:
        if "non-fast-forward" in err or "fetch first" in err:
            print("Push rejected: Remote has newer commits (non-fast-forward).", file=sys.stderr)
            print("Action needed: Run 'python3 scripts/code_sync.py reconcile' to integrate changes.", file=sys.stderr)
        else:
            print(f"Push failed: {err}", file=sys.stderr)
        return 1

    print("Publish successful.")
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    cwd = Path(args.repo) if args.repo else Path.cwd()
    if not is_repo_root(cwd):
        print("Error: Not inside a git repository", file=sys.stderr)
        return 1

    # Safe guard: reject reconcile on dirty working tree
    dirty_files = get_dirty_files(cwd)
    if dirty_files:
        print("Error: Cannot reconcile with dirty working tree.", file=sys.stderr)
        print("Please commit or stash your changes before pulling/merging.", file=sys.stderr)
        for f in dirty_files[:5]:
            print(f"  {f}", file=sys.stderr)
        return 1

    remote = args.remote
    if not check_remote_reachable(remote=remote, cwd=cwd):
        print(f"Error: Remote '{remote}' is unreachable (offline).", file=sys.stderr)
        return 2

    # Fetch remote refs
    print(f"Fetching from {remote}...")
    code, _, err = run_git(["fetch", remote], cwd=cwd)
    if code != 0:
        print(f"Error fetching from remote: {err}", file=sys.stderr)
        return 1

    branch = get_current_branch(cwd)
    remote_ref = f"{remote}/{args.branch or branch}"

    # Verify remote ref exists
    code, _, _ = run_git(["rev-parse", "--verify", remote_ref], cwd=cwd)
    if code != 0:
        print(f"Remote branch '{remote_ref}' does not exist yet. Nothing to reconcile.")
        return 0

    # Attempt merge
    print(f"Merging {remote_ref} into {branch}...")
    code, out, err = run_git(["merge", "--no-edit", remote_ref], cwd=cwd)
    if code == 0:
        print("Reconciliation successful: Up to date.")
        return 0

    # Conflict detected!
    conflict_code, conflict_out, _ = run_git(["diff", "--name-only", "--diff-filter=U"], cwd=cwd)
    conflicted_files = conflict_out.splitlines() if conflict_code == 0 else []

    print("\n" + "!" * 60, file=sys.stderr)
    print("MERGE CONFLICT DETECTED!", file=sys.stderr)
    print("Local and remote commits are preserved intact.", file=sys.stderr)
    print("Conflicted files:", file=sys.stderr)
    for cf in conflicted_files:
        print(f"  - {cf}", file=sys.stderr)
    print("\nResolution instructions:", file=sys.stderr)
    print("1. Edit the files above to resolve the conflict markers (<<<<<<<, =======, >>>>>>>).", file=sys.stderr)
    print("2. Run 'git add <file>' for each resolved file.", file=sys.stderr)
    print("3. Run 'git commit -m \"Resolve merge conflict\"'.", file=sys.stderr)
    print("4. Re-run 'code_sync.py publish'.", file=sys.stderr)
    print("!" * 60 + "\n", file=sys.stderr)
    return 3


def main() -> None:
    parser = argparse.ArgumentParser(description="ESP32 Safe Code Sync Tool")
    parser.add_argument("--repo", "-r", default=None, help="Path to git repository root")
    parser.add_argument("--remote", default="origin", help="Remote name (default: origin)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # status
    p_status = subparsers.add_parser("status", help="Inspect git synchronization status")
    p_status.add_argument("--no-network", action="store_true", help="Skip remote connectivity check")

    # publish
    p_pub = subparsers.add_parser("publish", help="Publish local branch to remote bare repo")
    p_pub.add_argument("--auto-commit", "-a", action="store_true", help="Auto-commit dirty tree before push")
    p_pub.add_argument("-m", "--message", default=None, help="Commit message if auto-committing")
    p_pub.add_argument("--branch", "-b", default=None, help="Target remote branch (default: same as current)")

    # reconcile
    p_rec = subparsers.add_parser("reconcile", help="Fetch and safely integrate remote changes")
    p_rec.add_argument("--branch", "-b", default=None, help="Remote branch to merge (default: same as current)")

    args = parser.parse_args()

    if args.command == "status":
        sys.exit(cmd_status(args))
    elif args.command == "publish":
        sys.exit(cmd_publish(args))
    elif args.command == "reconcile":
        sys.exit(cmd_reconcile(args))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
