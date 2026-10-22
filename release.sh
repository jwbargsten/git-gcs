#!/usr/bin/env bash

set -o errexit
set -o pipefail
set -o nounset

TAG=${1:-}
BRANCH=main

die() {
  echo "$*" >&2
  exit 1
}

confirm() {
  # https://stackoverflow.com/questions/1885525/how-do-i-prompt-a-user-for-confirmation-in-bash-script
  read -p "${1:-Are you sure?} (y/n) " -n 1 -r
  echo
  [[ $REPLY =~ ^[Yy]$ ]]
}

confirm_or_exit() {
  if ! confirm "$@"; then
    exit 0
  fi
}

[[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "usage: $0 vX.Y.Z"

# setuptools_scm turns any local change into a +g<sha>.d<date> version, which PyPI rejects
[[ -z "$(git status --porcelain)" ]] || die "working tree is not clean"
[[ "$(git branch --show-current)" == "$BRANCH" ]] || die "not on $BRANCH"
git fetch --quiet origin "$BRANCH"
[[ "$(git rev-parse HEAD)" == "$(git rev-parse "origin/$BRANCH")" ]] || die "$BRANCH is not in sync with origin/$BRANCH"
if git rev-parse --quiet --verify "refs/tags/$TAG" >/dev/null; then
  die "tag $TAG already exists locally"
fi
rc=0
git ls-remote --exit-code --tags origin "refs/tags/$TAG" >/dev/null || rc=$?
[[ $rc -eq 2 ]] || die "tag $TAG already exists on origin, or origin is unreachable"

echo "make dist runs lint and unit tests only, run make test-integration before releasing"
confirm_or_exit "Release $TAG from $(git rev-parse --short HEAD)?"

# setuptools_scm takes the version from the tag, so it has to exist before building.
# Until it is pushed, any failure or "no" removes it again.
git tag -a "$TAG" -m "$TAG"
trap 'echo "release aborted, removing local tag $TAG" >&2; git tag -d "$TAG" >/dev/null' EXIT

make dist
confirm_or_exit "Publish $TAG to PyPI?"

# Tag first: a pushed tag without an upload is easy to fix, an upload without a tag is not.
git push origin "$TAG"
trap - EXIT

make publish || die "upload failed, tag $TAG is already pushed: fix the cause and retry with 'make publish'"

# if you ever need to delete a tag: git tag -d v0.0.1 && git push origin :refs/tags/v0.0.1
