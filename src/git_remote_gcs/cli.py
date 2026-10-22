# pyright: basic

import argparse
import sys

from google.auth.exceptions import GoogleAuthError

from git_remote_gcs import gcs
from git_remote_gcs.infra import init_logging


def check_head(repo: gcs.Repo, refs: dict[str, str]) -> None:
    head = gcs.get_head(repo)
    if head is None:
        print("WARNING: no HEAD set")
        return
    print(f"HEAD -> {head}")
    if head in refs:
        return

    print(f"\nWARNING: HEAD points to non-existent ref '{head}'")
    choices = sorted(refs)
    for i, ref in enumerate(choices):
        print(f"  [{i}] {ref}")
    try:
        choice = input("Select new HEAD ref number (or Enter to skip): ").strip()
        if choice:
            gcs.set_head(repo, choices[int(choice)])
            print(f"HEAD updated to {choices[int(choice)]}")
    except (ValueError, IndexError, EOFError):
        pass


def check_conflicts(repo: gcs.Repo, delete_extra: bool, lock_ttl: int) -> None:
    print("\nChecking for conflicts...")
    conflicts = {ref: bundles for ref, bundles in gcs.list_all_bundles(repo).items() if len(bundles) > 1}
    if not conflicts:
        print("  No conflicts found.")
        return

    for ref, bundles in conflicts.items():
        # same rule as gcs.list_refs, so doctor keeps what clients already see
        keep = gcs.pick_newest(bundles)
        print(f"\n  CONFLICT: {ref} has {len(bundles)} bundles, clients see {keep.sha[:12]}:")
        for b in bundles:
            print(f"    - {b.key} ({b.updated.isoformat()})")
        if not delete_extra:
            print("    Run with --delete-bundle to keep only the newest bundle.")
            continue
        try:
            with gcs.ref_lock(repo, ref, lock_ttl):
                for b in bundles:
                    if b.key != keep.key:
                        print(f"    Deleting: {b.key}")
                        gcs.delete_blob(repo, b.key)
        except gcs.RefLockError as ex:
            print(f"    Skipped: {ex}")


def check_locks(repo: gcs.Repo, lock_ttl: int, clear: bool) -> None:
    print("\nChecking for stale locks...")
    stale = [lock for lock in gcs.list_locks(repo) if lock.age > lock_ttl]
    if not stale:
        print("  No stale locks found.")
        return
    for lock in stale:
        print(f"  STALE LOCK: {lock.key} (age: {lock.age:.0f}s, host: {lock.host})")
        if clear:
            removed = gcs.delete_lock(repo, lock)
            print("    Removed." if removed else "    Not removed, lock changed in the meantime.")
    if not clear:
        print("  Run with --clear-locks to remove them.")


def doctor_cmd(args: argparse.Namespace) -> None:
    repo = gcs.Repo.from_uri(args.uri)
    print(f"Analyzing repository at {args.uri}\n")

    refs = gcs.list_refs(repo)
    check_head(repo, refs)
    if not refs:
        print("No refs found. Repository appears empty.")
        return

    print(f"\nFound {len(refs)} ref(s):")
    for ref, sha in refs.items():
        print(f"  {ref} -> {sha[:12]}{' [PROTECTED]' if gcs.is_protected(repo, ref) else ''}")

    check_conflicts(repo, args.delete_bundle, args.lock_ttl)
    check_locks(repo, args.lock_ttl, args.clear_locks)
    print("\nDone.")


def protect_cmd(args: argparse.Namespace) -> None:
    gcs.protect_ref(gcs.Repo.from_uri(args.uri), f"refs/heads/{args.branch}")
    print(f"Protected branch: {args.branch}")


def unprotect_cmd(args: argparse.Namespace) -> None:
    gcs.unprotect_ref(gcs.Repo.from_uri(args.uri), f"refs/heads/{args.branch}")
    print(f"Unprotected branch: {args.branch}")


def delete_branch_cmd(args: argparse.Namespace) -> None:
    repo = gcs.Repo.from_uri(args.uri)
    ref = f"refs/heads/{args.branch}"

    with gcs.ref_lock(repo, ref, args.lock_ttl):
        if gcs.is_protected(repo, ref):
            sys.exit(f"Error: branch '{args.branch}' is protected. Unprotect it first.")
        if not gcs.list_ref_bundles(repo, ref):
            sys.exit(f"Branch '{args.branch}' not found on remote.")
        gcs.delete_ref(repo, ref)
    print(f"Deleted branch: {args.branch}")


def branch_name(value: str) -> str:
    if not gcs.is_valid_ref(f"refs/heads/{value}"):
        raise argparse.ArgumentTypeError(f"invalid branch name '{value}'")
    return value


# this approach is taken from the official python docs:
# https://docs.python.org/3/library/argparse.html#sub-commands
def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="git-gcs", description="Management tools for git-remote-gcs repositories"
    )
    subparsers = parser.add_subparsers(required=True)

    doctor_parser = subparsers.add_parser("doctor", help="Diagnose and fix repository issues")
    doctor_parser.add_argument("uri", help="GCS URI (gcs://bucket/prefix)")
    doctor_parser.add_argument(
        "--delete-bundle", action="store_true", help="Keep only the newest bundle per ref"
    )
    doctor_parser.add_argument("--clear-locks", action="store_true", help="Remove locks older than --lock-ttl")
    doctor_parser.add_argument(
        "--lock-ttl", type=int, default=60, help="Lock TTL in seconds for stale lock detection (default: 60)"
    )
    doctor_parser.set_defaults(func=doctor_cmd)

    protect_parser = subparsers.add_parser("protect", help="Protect a branch")
    protect_parser.add_argument("uri", help="GCS URI (gcs://bucket/prefix)")
    protect_parser.add_argument("branch", type=branch_name, help="Branch name to protect")
    protect_parser.set_defaults(func=protect_cmd)

    unprotect_parser = subparsers.add_parser("unprotect", help="Unprotect a branch")
    unprotect_parser.add_argument("uri", help="GCS URI (gcs://bucket/prefix)")
    unprotect_parser.add_argument("branch", type=branch_name, help="Branch name to unprotect")
    unprotect_parser.set_defaults(func=unprotect_cmd)

    delete_parser = subparsers.add_parser("delete-branch", help="Delete a remote branch")
    delete_parser.add_argument("uri", help="GCS URI (gcs://bucket/prefix)")
    delete_parser.add_argument("-b", "--branch", type=branch_name, required=True, help="Branch to delete")
    delete_parser.add_argument("--lock-ttl", type=int, default=60, help="Lock TTL in seconds (default: 60)")
    delete_parser.set_defaults(func=delete_branch_cmd)

    return parser


def main() -> None:
    init_logging(verbose=False)
    args = create_parser().parse_args()
    try:
        args.func(args)
    except (ValueError, gcs.RefLockError, GoogleAuthError) as ex:
        sys.exit(f"Error: {ex}")
