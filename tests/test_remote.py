# pyright: basic

import io
from datetime import timedelta

import pytest
from conftest import commit, run_git

from git_remote_gcs import gcs, remote

URI = "gcs://bucket/my-repo"


def _push(repo, conf, refspec):
    return remote.push_refspec(repo, conf, URI, refspec)


def test_push_fetch_roundtrip(repo, conf, work_dir, clone_dir, monkeypatch):
    sha = commit(work_dir, "a")

    assert _push(repo, conf, "refs/heads/main:refs/heads/main") == "ok refs/heads/main"
    assert remote.list_cmd(repo) == [f"{sha} refs/heads/main", "@refs/heads/main HEAD"]
    assert [k for k in repo.bucket.objects if k.endswith("LOCK.lock")] == []

    monkeypatch.chdir(clone_dir)
    remote.fetch_ref(repo, "refs/heads/main", sha)
    assert run_git(clone_dir, "cat-file", "-t", sha) == "commit"


def test_non_fast_forward_rejected(repo, conf, work_dir):
    commit(work_dir, "a")
    _push(repo, conf, "refs/heads/main:refs/heads/main")
    run_git(work_dir, "checkout", "-q", "--orphan", "other")
    diverged = commit(work_dir, "b")

    status = _push(repo, conf, "refs/heads/other:refs/heads/main")
    assert status.startswith("error refs/heads/main non-fast-forward")

    assert _push(repo, conf, "+refs/heads/other:refs/heads/main") == "ok refs/heads/main"
    assert gcs.list_refs(repo) == {"refs/heads/main": diverged}


def test_force_push_to_protected_rejected(repo, conf, work_dir):
    commit(work_dir, "a")
    _push(repo, conf, "refs/heads/main:refs/heads/main")
    gcs.protect_ref(repo, "refs/heads/main")

    assert "protected" in _push(repo, conf, "+refs/heads/main:refs/heads/main")
    assert "protected" in _push(repo, conf, ":refs/heads/main")


def test_push_rejects_nested_ref(repo, conf, work_dir):
    commit(work_dir, "a")
    _push(repo, conf, "refs/heads/main:refs/heads/foo/bar")

    assert "conflicts with existing ref refs/heads/foo/bar" in _push(
        repo, conf, "refs/heads/main:refs/heads/foo"
    )


def test_delete_ref_keeps_nested_protected_ref(repo, conf, work_dir):
    sha = commit(work_dir, "a")
    _push(repo, conf, "refs/heads/main:refs/heads/foo/bar")
    gcs.protect_ref(repo, "refs/heads/foo/bar")

    assert _push(repo, conf, ":refs/heads/foo") == "ok refs/heads/foo"
    assert gcs.list_refs(repo) == {"refs/heads/foo/bar": sha}
    assert gcs.is_protected(repo, "refs/heads/foo/bar")


def test_repush_of_stored_sha_keeps_bundle(repo, conf, work_dir):
    sha = commit(work_dir, "a")
    _push(repo, conf, "refs/heads/main:refs/heads/main")
    # a leftover newer bundle, e.g. from a cleanup that failed
    other = "f" * 40
    repo.bucket.blob(f"my-repo/refs/heads/main/{other}.bundle").upload_from_string("x")
    repo.bucket.objects[f"my-repo/refs/heads/main/{sha}.bundle"].updated -= timedelta(seconds=10)

    assert _push(repo, conf, "+refs/heads/main:refs/heads/main") == "ok refs/heads/main"
    assert gcs.list_refs(repo) == {"refs/heads/main": sha}


def test_push_blocked_by_fresh_lock(repo, conf, work_dir):
    commit(work_dir, "a")
    gcs.acquire_lock(repo, "refs/heads/main", ttl=60)

    status = _push(repo, conf, "refs/heads/main:refs/heads/main")
    assert status.startswith('error refs/heads/main "failed to acquire ref lock')
    assert "\n" not in status


def test_push_fails_if_lock_lost_during_upload(repo, conf, work_dir, monkeypatch):
    commit(work_dir, "a")
    upload = gcs.upload_bundle

    def _upload_and_lose_lock(repo, ref, sha, path):
        key = upload(repo, ref, sha, path)
        del repo.bucket.objects[repo.key(ref, gcs.LOCK_NAME)]
        gcs.acquire_lock(repo, ref, ttl=60)
        return key

    monkeypatch.setattr(gcs, "upload_bundle", _upload_and_lose_lock)

    assert "lock expired during upload" in _push(repo, conf, "refs/heads/main:refs/heads/main")
    assert gcs.acquire_lock(repo, "refs/heads/main", ttl=60) is None


def test_invalid_refspec(repo, conf):
    assert _push(repo, conf, "refs/heads/main") == "error refs/heads/main invalid refspec"
    assert _push(repo, conf, "refs/heads/main:refs/heads/../x") == "error refs/heads/../x invalid refspec"


def test_fetch_fails_on_corrupt_bundle(repo, work_dir):
    sha = "a" * 40
    repo.bucket.blob(f"my-repo/refs/heads/main/{sha}.bundle").upload_from_string("garbage")

    with pytest.raises(remote.FetchError):
        remote.fetch_ref(repo, "refs/heads/main", sha)


def test_option_cmd():
    assert remote.option_cmd("option verbosity 1") == "ok"
    assert remote.option_cmd("option verbosity x") == "unsupported"
    assert remote.option_cmd("option progress true") == "unsupported"


def test_protocol_session(repo, conf, work_dir):
    sha = commit(work_dir, "a")
    stdin = io.StringIO("capabilities\npush refs/heads/main:refs/heads/main\n\nlist\n\n")
    stdout = io.StringIO()

    remote.run(repo, conf, URI, stdin, stdout)

    assert stdout.getvalue() == (
        f"push\nfetch\noption\n\nok refs/heads/main\n\n{sha} refs/heads/main\n@refs/heads/main HEAD\n\n"
    )
