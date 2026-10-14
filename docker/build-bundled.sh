#!/usr/bin/env bash
# Build from a verified, locally available Wikipedia data image.
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
data_image=${WIKIPEDIA_DATA_IMAGE:-drbenchpro/wiki-search-enwiki:20260701-v1-bundled}
expected=${WIKIPEDIA_DATA_IMAGE_ID:?Set WIKIPEDIA_DATA_IMAGE_ID to the verified full sha256 image ID}
builder=${DRBP_BUILDER:-$(docker context show)}
version=$(sed -n 's/^version = "\([^"]*\)"/\1/p' "$root/pyproject.toml")
tag=${DRBP_IMAGE:-drbenchpro/standalone:${version}-enwiki-20260701-v1}
revision=${DRBP_SOURCE_REVISION:-}
if [[ -z "$revision" ]]; then
  revision=$(git -C "$root" rev-parse HEAD)
  git -C "$root" diff --quiet
  git -C "$root" diff --cached --quiet
  if [[ -n "$(git -C "$root" ls-files --others --exclude-standard)" ]]; then
    echo 'Commit public source files before building; ignored .local material is excluded.' >&2
    exit 2
  fi
fi
[[ "$revision" =~ ^[0-9a-f]{40}$ ]] || { echo 'A full source commit is required.' >&2; exit 2; }
[[ "$expected" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo 'A full data image ID is required.' >&2; exit 2; }
actual=$(docker image inspect --format '{{.Id}}' "$data_image")
[[ "$actual" == "$expected" ]] || { echo 'Data image ID differs; refusing to build.' >&2; exit 2; }
[[ $(docker image inspect --platform linux/amd64 --format '{{.Os}}/{{.Architecture}}' "$data_image") == linux/amd64 ]] || {
  echo 'This build requires a verified linux/amd64 data image.' >&2; exit 2;
}
driver=$(docker buildx inspect "$builder" | sed -n 's/^Driver:[[:space:]]*//p')
[[ "$driver" == docker ]] || { echo 'Choose a docker-driver builder with access to the local data image.' >&2; exit 2; }
# A content-derived local alias avoids using a mutable source tag during the build.
alias_image="drbenchpro/wiki-data-build:${expected#sha256:}"
docker image tag "$actual" "$alias_image"
python_image=${PYTHON_IMAGE:-python:3.12-slim}
if ! docker image inspect "$python_image" >/dev/null 2>&1; then
  docker pull --platform linux/amd64 "$python_image"
fi
[[ $(docker image inspect --platform linux/amd64 --format '{{.Os}}/{{.Architecture}}' "$python_image") == linux/amd64 ]] || {
  echo 'Python base must be linux/amd64; pull it with --platform linux/amd64.' >&2; exit 2;
}
python_digest=$(docker image inspect --platform linux/amd64 --format '{{range .RepoDigests}}{{println .}}{{end}}' "$python_image" | head -n 1)
[[ -n "$python_digest" ]] || { echo 'Python base needs a repository digest.' >&2; exit 2; }
printf 'Source commit: %s\nData image ID: %s\nPython base: %s\nOutput tag: %s\n' "$revision" "$actual" "$python_digest" "$tag"
docker buildx build --builder "$builder" --load --platform linux/amd64 \
  -f "$root/Dockerfile.bundled" \
  --build-arg "WIKIPEDIA_DATA_IMAGE=$alias_image" \
  --build-arg "PYTHON_IMAGE=$python_digest" \
  --build-arg "SOURCE_REVISION=$revision" --build-arg "APP_VERSION=$version" \
  -t "$tag" "$root"
docker image inspect --format 'Image ID: {{.Id}}; platform: {{.Os}}/{{.Architecture}}; size: {{.Size}} bytes' "$tag"
