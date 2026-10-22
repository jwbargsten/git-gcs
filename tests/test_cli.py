# pyright: basic

from datetime import timedelta

from git_remote_gcs import cli, gcs


def _stale_lock(repo):
    lock = gcs.acquire_lock(repo, "refs/heads/main", ttl=60)
    repo.bucket.objects[lock.key].time_created -= timedelta(seconds=120)
    return lock


def test_doctor_keeps_stale_locks_by_default(repo):
    lock = _stale_lock(repo)

    cli.check_locks(repo, lock_ttl=60, clear=False)

    assert gcs.holds_lock(repo, lock)


def test_doctor_clears_stale_locks(repo):
    lock = _stale_lock(repo)

    cli.check_locks(repo, lock_ttl=60, clear=True)

    assert not gcs.holds_lock(repo, lock)


def test_doctor_delete_bundle_keeps_what_clients_see(repo):
    older, newer = "a" * 40, "b" * 40
    repo.bucket.blob(f"my-repo/refs/heads/main/{newer}.bundle").upload_from_string("")
    repo.bucket.blob(f"my-repo/refs/heads/main/{older}.bundle").upload_from_string("")
    repo.bucket.objects[f"my-repo/refs/heads/main/{older}.bundle"].updated -= timedelta(seconds=10)
    assert gcs.list_refs(repo) == {"refs/heads/main": newer}

    cli.check_conflicts(repo, delete_extra=True, lock_ttl=60)

    assert [b.sha for b in gcs.list_ref_bundles(repo, "refs/heads/main")] == [newer]
