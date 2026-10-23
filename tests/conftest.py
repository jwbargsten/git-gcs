# pyright: basic

import itertools
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
from google.api_core import exceptions as gcs_exceptions

from git_gcs import gcs
from git_gcs.infra import Config

_generations = itertools.count(1)


@dataclass
class FakeObject:
    data: bytes
    generation: int = field(default_factory=lambda: next(_generations))
    time_created: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated: datetime = field(default_factory=lambda: datetime.now(UTC))


class FakeBlob:
    """The subset of google.cloud.storage.Blob that git_gcs uses, including generation preconditions"""

    def __init__(self, bucket: "FakeBucket", name: str, obj: FakeObject | None = None):
        self.bucket = bucket
        self.name = name
        self.generation = obj.generation if obj else None
        self.time_created = obj.time_created if obj else None
        self.updated = obj.updated if obj else None

    def _load(self, obj: FakeObject) -> None:
        self.generation, self.time_created, self.updated = obj.generation, obj.time_created, obj.updated

    def _current(self) -> FakeObject:
        obj = self.bucket.objects.get(self.name)
        if obj is None or (self.generation is not None and obj.generation != self.generation):
            raise gcs_exceptions.NotFound(self.name)
        return obj

    def upload_from_string(self, data, content_type=None, if_generation_match=None):
        current = self.bucket.objects.get(self.name)
        if if_generation_match is not None and (current.generation if current else 0) != if_generation_match:
            raise gcs_exceptions.PreconditionFailed(self.name)
        obj = FakeObject(data.encode() if isinstance(data, str) else data)
        self.bucket.objects[self.name] = obj
        self._load(obj)

    def upload_from_filename(self, filename):
        self.upload_from_string(Path(filename).read_bytes())

    def download_as_text(self):
        return self._current().data.decode()

    def download_to_filename(self, filename):
        Path(filename).write_bytes(self._current().data)

    def delete(self, if_generation_match=None):
        current = self.bucket.objects.get(self.name)
        if current is None:
            raise gcs_exceptions.NotFound(self.name)
        if if_generation_match is not None and current.generation != if_generation_match:
            raise gcs_exceptions.PreconditionFailed(self.name)
        del self.bucket.objects[self.name]

    def reload(self):
        obj = self.bucket.objects.get(self.name)
        if obj is None:
            raise gcs_exceptions.NotFound(self.name)
        self._load(obj)

    def exists(self):
        return self.name in self.bucket.objects


class FakeBucket:
    def __init__(self):
        self.objects: dict[str, FakeObject] = {}

    def blob(self, name):
        return FakeBlob(self, name)

    def list_blobs(self, prefix=""):
        return [FakeBlob(self, k, o) for k, o in sorted(self.objects.items()) if k.startswith(prefix)]


@pytest.fixture
def repo() -> gcs.Repo:
    return gcs.Repo(bucket=FakeBucket(), prefix="my-repo")  # pyright: ignore[reportArgumentType]


@pytest.fixture
def conf() -> Config:
    return Config(verbose=False, lock_ttl=60)


def run_git(cwd: Path, *args: str) -> str:
    res = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        pytest.fail(f"git {' '.join(args)} failed ({res.returncode}):\n{res.stderr}")
    return res.stdout.strip()


def commit(cwd: Path, name: str) -> str:
    (cwd / name).write_text(name)
    run_git(cwd, "add", name)
    run_git(cwd, "commit", "-q", "-m", name)
    return run_git(cwd, "rev-parse", "HEAD")


@pytest.fixture
def git_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(var, "test")
    for var in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(var, "test@example.com")


@pytest.fixture
def work_dir(tmp_path, monkeypatch, git_env) -> Path:
    path = tmp_path / "work"
    path.mkdir()
    run_git(path, "init", "-q", "-b", "main")
    monkeypatch.chdir(path)
    return path


@pytest.fixture
def clone_dir(tmp_path, git_env) -> Path:
    path = tmp_path / "clone"
    path.mkdir()
    run_git(path, "init", "-q", "-b", "main")
    return path
