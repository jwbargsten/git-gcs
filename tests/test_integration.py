# pyright: basic

import os
import subprocess
import sys
import uuid

import pytest
from conftest import commit, run_git
from google.api_core import exceptions as gcs_exceptions
from google.cloud import storage

from git_remote_gcs import gcs

pytestmark = pytest.mark.skipif(
    "STORAGE_EMULATOR_HOST" not in os.environ, reason="needs a fake GCS server, see `make fake-gcs`"
)

BUCKET = "git-remote-gcs-test"


@pytest.fixture(scope="module")
def bucket():
    client = storage.Client()
    try:
        return client.create_bucket(BUCKET)
    except gcs_exceptions.Conflict:
        return client.bucket(BUCKET)


@pytest.fixture
def uri(bucket, git_env, monkeypatch) -> str:
    # git looks up git-remote-gcs on PATH, use the one installed in this venv
    monkeypatch.setenv("PATH", f"{os.path.dirname(sys.executable)}{os.pathsep}{os.environ['PATH']}")
    return f"gcs://{BUCKET}/{uuid.uuid4().hex}"


@pytest.fixture
def work_dir(tmp_path, uri):
    path = tmp_path / "work"
    path.mkdir()
    run_git(path, "init", "-q", "-b", "main")
    run_git(path, "remote", "add", "origin", uri)
    return path


def _git_status(cwd, *args) -> int:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=False).returncode


def _remote_refs(cwd) -> dict[str, str]:
    lines = run_git(cwd, "ls-remote", "origin").splitlines()
    return {ref: sha for sha, ref in (line.split("\t") for line in lines) if ref != "HEAD"}


def test_push_and_clone(work_dir, uri, tmp_path):
    sha = commit(work_dir, "a")
    run_git(work_dir, "tag", "v1")
    run_git(work_dir, "push", "-q", "origin", "main", "v1")

    run_git(tmp_path, "clone", "-q", uri, "clone")

    clone = tmp_path / "clone"
    assert run_git(clone, "rev-parse", "HEAD") == sha
    assert (clone / "a").read_text() == "a"
    assert run_git(clone, "rev-parse", "v1") == sha


def test_non_fast_forward_needs_force(work_dir, uri, tmp_path):
    commit(work_dir, "a")
    run_git(work_dir, "push", "-q", "origin", "main")
    run_git(tmp_path, "clone", "-q", uri, "other")
    other = tmp_path / "other"

    pushed = commit(work_dir, "b")
    run_git(work_dir, "push", "-q", "origin", "main")
    diverged = commit(other, "c")

    assert _git_status(other, "push", "origin", "main") != 0
    assert _remote_refs(work_dir) == {"refs/heads/main": pushed}

    run_git(other, "push", "-q", "--force", "origin", "main")
    assert _remote_refs(work_dir) == {"refs/heads/main": diverged}


def test_delete_branch(work_dir):
    commit(work_dir, "a")
    run_git(work_dir, "push", "-q", "origin", "main", "main:feature")

    run_git(work_dir, "push", "-q", "origin", ":feature")

    assert list(_remote_refs(work_dir)) == ["refs/heads/main"]


def test_protected_branch(work_dir, uri):
    sha = commit(work_dir, "a")
    run_git(work_dir, "push", "-q", "origin", "main")
    subprocess.run(["git-gcs", "protect", uri, "main"], check=True, capture_output=True)
    run_git(work_dir, "commit", "-q", "--amend", "-m", "amended")

    assert _git_status(work_dir, "push", "--force", "origin", "main") != 0
    assert _git_status(work_dir, "push", "origin", ":main") != 0
    assert _remote_refs(work_dir) == {"refs/heads/main": sha}


def test_lock_is_exclusive(bucket):
    # The unit tests rely on FakeBucket behaving like GCS here, so check it against the API.
    # Releasing a lock that someone else took over is only covered by the unit tests:
    # fake-gcs-server (1.56) ignores ifGenerationMatch on delete, real GCS does not.
    repo = gcs.Repo(bucket=bucket, prefix=uuid.uuid4().hex)
    lock = gcs.acquire_lock(repo, "refs/heads/main", ttl=60)
    assert lock is not None
    assert gcs.acquire_lock(repo, "refs/heads/main", ttl=60) is None

    gcs.release_lock(repo, lock)
    assert gcs.acquire_lock(repo, "refs/heads/main", ttl=60) is not None
