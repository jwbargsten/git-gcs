"""
Storage layout in GCS:
    <prefix>/HEAD                              - contains the default branch ref
    <prefix>/<ref>/<sha>.bundle                - git bundle for a ref, e.g. refs/heads/main
    <prefix>/<ref>/LOCK.lock                   - lock for concurrent push protection
    <prefix>/<ref>/PROTECTED                   - marker for protected branches
"""

import json
import logging
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from google.api_core import exceptions as gcs_exceptions
from google.cloud import storage

logger = logging.getLogger(__name__)

SHA_RE = re.compile(r"[0-9a-f]{40}")
LOCK_NAME = "LOCK.lock"
PROTECTED_NAME = "PROTECTED"


class RefLockError(Exception):
    pass


@dataclass(frozen=True)
class Repo:
    bucket: storage.Bucket
    prefix: str

    @classmethod
    def from_uri(cls, uri: str) -> "Repo":
        bucket, prefix = parse_uri(uri)
        return cls(bucket=storage.Client().bucket(bucket), prefix=prefix)

    def key(self, *parts: str) -> str:
        return "/".join(p for p in (self.prefix, *parts) if p)


@dataclass(frozen=True)
class Bundle:
    ref: str
    sha: str
    key: str
    updated: datetime


@dataclass(frozen=True)
class Lock:
    key: str
    generation: int


@dataclass(frozen=True)
class LockInfo:
    key: str
    generation: int
    age: float
    host: str


def parse_uri(uri: str) -> tuple[str, str]:
    match = re.fullmatch(r"gcs://([^/]+)(?:/(.+))?", uri.rstrip("/"))
    if not match:
        raise ValueError(f"invalid GCS URI: {uri}")
    return match.group(1), match.group(2) or ""


def is_valid_ref(ref: str) -> bool:
    """Subset of git check-ref-format rules, enough to reject anything git or our layout would trip on"""
    if not ref.startswith("refs/") or ref.endswith(("/", ".", ".lock")):
        return False
    if any(ord(c) < 0x20 or c in " ~^:?*[\\\x7f" for c in ref) or ".." in ref or "@{" in ref or "//" in ref:
        return False
    return not any(part.startswith(".") or part.endswith(".lock") for part in ref.split("/"))


def is_nested(a: str, b: str) -> bool:
    return a.startswith(b + "/") or b.startswith(a + "/")


def _rel(repo: Repo, key: str) -> str:
    return key[len(repo.prefix) + 1 :] if repo.prefix else key


def _parse_bundle(repo: Repo, blob: storage.Blob) -> Bundle | None:
    ref, _, filename = _rel(repo, blob.name).rpartition("/")
    sha = filename.removesuffix(".bundle")
    if not filename.endswith(".bundle") or not SHA_RE.fullmatch(sha) or not is_valid_ref(ref):
        return None
    return Bundle(ref=ref, sha=sha, key=blob.name, updated=blob.updated)


def list_all_bundles(repo: Repo) -> dict[str, list[Bundle]]:
    bundles: dict[str, list[Bundle]] = {}
    for blob in repo.bucket.list_blobs(prefix=repo.key("refs/")):
        if (bundle := _parse_bundle(repo, blob)) is not None:
            bundles.setdefault(bundle.ref, []).append(bundle)
    return bundles


def list_ref_bundles(repo: Repo, ref: str) -> list[Bundle]:
    # The listing prefix also matches nested refs (refs/heads/foo/bar for refs/heads/foo), keep only our own.
    blobs = repo.bucket.list_blobs(prefix=repo.key(ref) + "/")
    return [b for blob in blobs if (b := _parse_bundle(repo, blob)) is not None and b.ref == ref]


def pick_newest(bundles: list[Bundle]) -> Bundle:
    return max(bundles, key=lambda b: b.updated)


def list_refs(repo: Repo) -> dict[str, str]:
    return {ref: pick_newest(bundles).sha for ref, bundles in sorted(list_all_bundles(repo).items())}


def get_head(repo: Repo) -> str | None:
    try:
        return repo.bucket.blob(repo.key("HEAD")).download_as_text().strip()
    except gcs_exceptions.NotFound:
        return None


def set_head(repo: Repo, ref: str) -> None:
    repo.bucket.blob(repo.key("HEAD")).upload_from_string(ref, content_type="text/plain")
    logger.debug("set HEAD -> %s", ref)


def upload_bundle(repo: Repo, ref: str, sha: str, local_path: Path) -> str:
    key = repo.key(ref, f"{sha}.bundle")
    repo.bucket.blob(key).upload_from_filename(str(local_path))
    logger.debug("uploaded bundle %s", key)
    return key


def download_bundle(repo: Repo, ref: str, sha: str, local_path: Path) -> None:
    key = repo.key(ref, f"{sha}.bundle")
    repo.bucket.blob(key).download_to_filename(str(local_path))
    logger.debug("downloaded bundle %s", key)


def delete_blob(repo: Repo, key: str) -> None:
    with suppress(gcs_exceptions.NotFound):
        repo.bucket.blob(key).delete()
    logger.debug("deleted %s", key)


def delete_ref(repo: Repo, ref: str) -> None:
    for bundle in list_ref_bundles(repo, ref):
        delete_blob(repo, bundle.key)
    if get_head(repo) == ref:
        delete_blob(repo, repo.key("HEAD"))
    logger.debug("deleted ref %s", ref)


def _age(created: datetime) -> float:
    # time_created is assigned by GCS, so only the local clock can skew this, not the clock of the lock holder
    return (datetime.now(UTC) - created).total_seconds()


def acquire_lock(repo: Repo, ref: str, ttl: int) -> Lock | None:
    key = repo.key(ref, LOCK_NAME)
    blob = repo.bucket.blob(key)
    data = json.dumps(
        {"pid": os.getpid(), "host": os.getenv("COMPUTERNAME", os.getenv("HOSTNAME", "unknown"))}
    )

    try:
        blob.upload_from_string(data, content_type="application/json", if_generation_match=0)
        return Lock(key=key, generation=blob.generation)
    except gcs_exceptions.PreconditionFailed:
        pass

    try:
        blob.reload()
    except gcs_exceptions.NotFound:
        return None
    age = _age(blob.time_created)
    if age <= ttl:
        logger.debug("lock %s is fresh (%.0fs)", key, age)
        return None

    logger.debug("replacing stale lock %s (%.0fs)", key, age)
    try:
        blob.delete(if_generation_match=blob.generation)
        blob.upload_from_string(data, content_type="application/json", if_generation_match=0)
    except (gcs_exceptions.PreconditionFailed, gcs_exceptions.NotFound):
        logger.debug("another client acquired lock %s first", key)
        return None
    return Lock(key=key, generation=blob.generation)


def holds_lock(repo: Repo, lock: Lock) -> bool:
    blob = repo.bucket.blob(lock.key)
    try:
        blob.reload()
    except gcs_exceptions.NotFound:
        return False
    return blob.generation == lock.generation


def release_lock(repo: Repo, lock: Lock) -> None:
    # If our lock expired and someone else took over, the generation check keeps us from deleting theirs.
    try:
        repo.bucket.blob(lock.key).delete(if_generation_match=lock.generation)
    except (gcs_exceptions.NotFound, gcs_exceptions.PreconditionFailed):
        logger.warning("lock %s was lost before release", lock.key)


@contextmanager
def ref_lock(repo: Repo, ref: str, ttl: int) -> Iterator[Lock]:
    lock = acquire_lock(repo, ref, ttl)
    if lock is None:
        raise RefLockError(f"failed to acquire ref lock at {repo.key(ref, LOCK_NAME)}")
    try:
        yield lock
    finally:
        release_lock(repo, lock)


def list_locks(repo: Repo) -> list[LockInfo]:
    locks = []
    for blob in repo.bucket.list_blobs(prefix=repo.key("refs/")):
        if not blob.name.endswith("/" + LOCK_NAME):
            continue
        try:
            host = json.loads(blob.download_as_text()).get("host", "unknown")
        except (gcs_exceptions.NotFound, ValueError, AttributeError):
            host = "unknown"
        locks.append(
            LockInfo(key=blob.name, generation=blob.generation, age=_age(blob.time_created), host=host)
        )
    return locks


def delete_lock(repo: Repo, lock: LockInfo) -> bool:
    try:
        repo.bucket.blob(lock.key).delete(if_generation_match=lock.generation)
        return True
    except (gcs_exceptions.NotFound, gcs_exceptions.PreconditionFailed):
        return False


def is_protected(repo: Repo, ref: str) -> bool:
    return repo.bucket.blob(repo.key(ref, PROTECTED_NAME)).exists()


def protect_ref(repo: Repo, ref: str) -> None:
    repo.bucket.blob(repo.key(ref, PROTECTED_NAME)).upload_from_string("", content_type="text/plain")


def unprotect_ref(repo: Repo, ref: str) -> None:
    delete_blob(repo, repo.key(ref, PROTECTED_NAME))
