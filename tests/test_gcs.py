# pyright: basic

from datetime import timedelta

import pytest

from git_remote_gcs import gcs

SHA_A = "a" * 40
SHA_B = "b" * 40


def _age_object(repo, key, seconds):
    obj = repo.bucket.objects[key]
    obj.time_created -= timedelta(seconds=seconds)
    obj.updated -= timedelta(seconds=seconds)


def test_parse_uri():
    assert gcs.parse_uri("gcs://bucket") == ("bucket", "")
    assert gcs.parse_uri("gcs://bucket/team/repo/") == ("bucket", "team/repo")
    with pytest.raises(ValueError):
        gcs.parse_uri("gs://bucket/repo")


def test_is_valid_ref():
    assert gcs.is_valid_ref("refs/heads/main")
    assert gcs.is_valid_ref("refs/heads/feature/x-1")
    assert gcs.is_valid_ref("refs/tags/v1.0")
    for ref in [
        "main",
        "refs/heads/../../HEAD",
        "refs/heads/a b",
        "refs/heads/x\nok",
        "refs/heads/main.lock",
        "refs/heads/.hidden",
        "refs/heads/",
        "refs/heads/a@{1}",
    ]:
        assert not gcs.is_valid_ref(ref), ref


def test_list_refs_picks_newest_bundle(repo):
    repo.bucket.blob(f"my-repo/refs/heads/main/{SHA_B}.bundle").upload_from_string("old")
    _age_object(repo, f"my-repo/refs/heads/main/{SHA_B}.bundle", 10)
    repo.bucket.blob(f"my-repo/refs/heads/main/{SHA_A}.bundle").upload_from_string("new")

    assert gcs.list_refs(repo) == {"refs/heads/main": SHA_A}


def test_list_ref_bundles_ignores_nested_ref(repo):
    repo.bucket.blob(f"my-repo/refs/heads/foo/{SHA_A}.bundle").upload_from_string("")
    repo.bucket.blob(f"my-repo/refs/heads/foo/bar/{SHA_B}.bundle").upload_from_string("")

    gcs.delete_ref(repo, "refs/heads/foo")

    assert gcs.list_refs(repo) == {"refs/heads/foo/bar": SHA_B}


def test_fresh_lock_blocks(repo):
    assert gcs.acquire_lock(repo, "refs/heads/main", ttl=60) is not None
    assert gcs.acquire_lock(repo, "refs/heads/main", ttl=60) is None


def test_release_keeps_foreign_lock(repo):
    first = gcs.acquire_lock(repo, "refs/heads/main", ttl=60)
    _age_object(repo, first.key, 120)
    second = gcs.acquire_lock(repo, "refs/heads/main", ttl=60)
    assert second is not None

    gcs.release_lock(repo, first)

    assert gcs.holds_lock(repo, second)
    assert not gcs.holds_lock(repo, first)


def test_ref_lock_releases(repo):
    with gcs.ref_lock(repo, "refs/heads/main", ttl=60):
        assert gcs.acquire_lock(repo, "refs/heads/main", ttl=60) is None
    assert repo.bucket.objects == {}
