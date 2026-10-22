"""
Git remote helper for Google Cloud Storage, see https://git-scm.com/docs/gitremote-helpers

Usage:
    git remote add origin gcs://my-bucket/my-repo
    git push origin main
    git clone gcs://my-bucket/my-repo
"""

import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TextIO

from google.api_core import exceptions as gcs_exceptions
from google.auth.exceptions import GoogleAuthError
from structlog import get_logger

from git_remote_gcs import gcs
from git_remote_gcs.infra import Config, ConfigLoadError, init_logging

logger = get_logger()

CAPABILITIES = ["push", "fetch", "option"]


class PushRejectedError(Exception):
    pass


class FetchError(Exception):
    pass


def git(*args: str) -> str:
    logger.debug("running git", args=args)
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout.strip()


def is_ancestor(ancestor: str, descendant: str) -> bool:
    res = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant], capture_output=True, check=False
    )
    return res.returncode == 0


def to_one_line(ex: BaseException) -> str:
    # git reads one status line per ref; a multi-line message would break the protocol
    return " ".join(str(ex).split()) or type(ex).__name__


def read_batch(first_line: str, stdin: TextIO) -> list[str]:
    lines = [first_line]
    while line := stdin.readline().strip():
        lines.append(line)
    return lines


def list_cmd(repo: gcs.Repo) -> list[str]:
    refs = gcs.list_refs(repo)
    head = gcs.get_head(repo)
    lines = [f"{sha} {ref}" for ref, sha in refs.items()]
    if head in refs:
        lines.append(f"@{head} HEAD")
    elif refs:
        lines.append(f"@{next(iter(refs))} HEAD")
    return lines


def fetch_ref(repo: gcs.Repo, ref: str, sha: str) -> None:
    if not gcs.is_valid_ref(ref) or not gcs.SHA_RE.fullmatch(sha):
        raise FetchError(f"refusing to fetch invalid ref '{ref}' ({sha})")
    logger.debug("fetching", ref=ref, sha=sha)
    with tempfile.TemporaryDirectory() as tmpdir:
        bundle_path = Path(tmpdir) / f"{sha}.bundle"
        try:
            gcs.download_bundle(repo, ref, sha, bundle_path)
            git("bundle", "verify", str(bundle_path))
            git("bundle", "unbundle", str(bundle_path))
        except subprocess.CalledProcessError as ex:
            raise FetchError(f"invalid bundle for {ref} ({sha}): {ex.stderr.strip()}") from ex
        except gcs_exceptions.GoogleAPIError as ex:
            raise FetchError(f"could not download bundle for {ref} ({sha})") from ex


def check_push_allowed(repo: gcs.Repo, dst: str, sha: str, force: bool) -> None:
    if force and gcs.is_protected(repo, dst):
        raise PushRejectedError("branch is protected, force push not allowed")
    remote_refs = gcs.list_refs(repo)
    if conflict := next((r for r in remote_refs if gcs.is_nested(r, dst)), None):
        raise PushRejectedError(f"conflicts with existing ref {conflict}")
    if not force and dst in remote_refs and not is_ancestor(remote_refs[dst], sha):
        raise PushRejectedError("non-fast-forward update, use --force to override")


def push_ref(repo: gcs.Repo, conf: Config, src: str, dst: str, force: bool) -> None:
    sha = git("rev-parse", "--verify", src)
    logger.debug("pushing", src=src, dst=dst, sha=sha)
    with tempfile.TemporaryDirectory() as tmpdir:
        # Bundle before locking: it does not depend on remote state and is the slow part for big repos.
        # SUGGESTION: incremental bundles (with the remote sha as prerequisite) instead of full history
        bundle_path = Path(tmpdir) / f"{sha}.bundle"
        git("bundle", "create", str(bundle_path), src)

        with gcs.ref_lock(repo, dst, conf.lock_ttl) as lock:
            # Checked under the lock, otherwise two concurrent pushes both pass and the second one wins.
            check_push_allowed(repo, dst, sha, force)
            old_bundles = gcs.list_ref_bundles(repo, dst)
            new_key = gcs.upload_bundle(repo, dst, sha, bundle_path)
            if not gcs.holds_lock(repo, lock):
                raise gcs.RefLockError(
                    f"lock expired during upload (ttl {conf.lock_ttl}s), keeping old bundles"
                )
            for bundle in old_bundles:
                if bundle.key != new_key:
                    gcs.delete_blob(repo, bundle.key)
            if gcs.get_head(repo) is None:
                gcs.set_head(repo, dst)


def delete_remote_ref(repo: gcs.Repo, conf: Config, dst: str) -> None:
    logger.debug("deleting remote ref", ref=dst)
    with gcs.ref_lock(repo, dst, conf.lock_ttl):
        if gcs.is_protected(repo, dst):
            raise PushRejectedError("branch is protected")
        gcs.delete_ref(repo, dst)


def push_refspec(repo: gcs.Repo, conf: Config, uri: str, refspec: str) -> str:
    force = refspec.startswith("+")
    src, sep, dst = refspec.removeprefix("+").partition(":")
    if not sep or not gcs.is_valid_ref(dst):
        return f"error {dst or refspec} invalid refspec"

    try:
        if src:
            push_ref(repo, conf, src, dst, force)
        else:
            delete_remote_ref(repo, conf, dst)
    except gcs.RefLockError as ex:
        return (
            f'error {dst} "{to_one_line(ex)}. Another client may be pushing. If this persists beyond '
            f'{conf.lock_ttl}s, run git-gcs doctor {uri} --clear-locks --lock-ttl {conf.lock_ttl}"'
        )
    except (
        PushRejectedError,
        subprocess.CalledProcessError,
        gcs_exceptions.GoogleAPIError,
        OSError,
    ) as ex:
        logger.debug("push failed", ref=dst, exc_info=ex)
        return f"error {dst} {to_one_line(ex)}"
    return f"ok {dst}"


def option_cmd(line: str) -> str:
    _, name, value = (line.split(" ", 2) + ["", ""])[:3]
    if name != "verbosity" or not value.isdigit():
        return "unsupported"
    if int(value) >= 2:
        init_logging(verbose=True)
    return "ok"


def reply(out: TextIO, lines: list[str]) -> None:
    out.write("".join(f"{line}\n" for line in lines))
    out.flush()


def run(repo: gcs.Repo, conf: Config, uri: str, stdin: TextIO, stdout: TextIO) -> None:
    while line := stdin.readline().strip():
        logger.debug("command", line=line)
        match line.split(" ", 1)[0]:
            case "capabilities":
                reply(stdout, [*CAPABILITIES, ""])
            case "option":
                reply(stdout, [option_cmd(line)])
            case "list":
                reply(stdout, [*list_cmd(repo), ""])
            case "fetch":
                for fetch_line in read_batch(line, stdin):
                    _, sha, ref = (fetch_line.split() + ["", ""])[:3]
                    fetch_ref(repo, ref, sha)
                reply(stdout, [""])
            case "push":
                refspecs = [push_line.split(" ", 1)[1] for push_line in read_batch(line, stdin)]
                reply(stdout, [*(push_refspec(repo, conf, uri, r) for r in refspecs), ""])
            case _:
                raise ValueError(f"unknown command: {line}")


def main() -> None:
    if len(sys.argv) < 3:
        sys.exit("Usage: git-remote-gcs <remote-name> <url>")
    uri = sys.argv[2]

    try:
        conf = Config.from_env()
        init_logging(conf.verbose)
        repo = gcs.Repo.from_uri(uri)
        run(repo, conf, uri, sys.stdin, sys.stdout)
    except (ConfigLoadError, ValueError, FetchError, GoogleAuthError) as ex:
        sys.exit(f"git-remote-gcs: {ex}")


if __name__ == "__main__":
    main()
