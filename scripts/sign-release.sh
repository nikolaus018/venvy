#!/usr/bin/env bash
# Signs and publishes a release the release workflow left as a draft.
#
#   scripts/sign-release.sh v4.8.0
#
# Run on the maintainer's machine, never in CI: the key is what proves a
# release came from here, so it must not be anywhere a GitHub login reaches.
# Before signing, the archive the workflow built is unpacked and compared
# with this checkout's tag, file for file: a signature vouches for code that
# was looked at, not for whatever the workflow happened to upload.
#
# The key: VENVY_RELEASE_KEY, or ~/.config/venvy/release-signing-key.pem
# (Ed25519, made with `openssl genpkey -algorithm ed25519`). Its public half
# is RELEASE_PUBKEY in install.sh. Keep a copy somewhere safe: without it no
# release can be published that existing servers will install.
set -euo pipefail
TAG="${1:?usage: scripts/sign-release.sh <tag, e.g. v4.8.0>}"
KEY="${VENVY_RELEASE_KEY:-$HOME/.config/venvy/release-signing-key.pem}"
cd "$(dirname "$0")/.."
[ -r "$KEY" ] || { echo "No release key at $KEY." >&2; exit 1; }

# The key must be the one install.sh trusts.
want="$(sed -n "/^RELEASE_PUBKEY='/,/END PUBLIC KEY/p" install.sh | sed "s/^RELEASE_PUBKEY='//; s/'$//")"
have="$(openssl pkey -in "$KEY" -pubout)"
[ "$want" = "$have" ] || { echo "$KEY is not the key in install.sh (RELEASE_PUBKEY)." >&2; exit 1; }

git fetch -q --tags origin
git rev-parse -q --verify "refs/tags/$TAG" >/dev/null || { echo "No tag $TAG." >&2; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
gh release download "$TAG" -p venvy.tar.gz -p venvy.tar.gz.sha256 -p install.sh -D "$WORK"
( cd "$WORK" && shasum -a 256 -c -s venvy.tar.gz.sha256 ) || { echo "The archive doesn't match its checksum." >&2; exit 1; }

# What the workflow built, against the tag as checked out here.
mkdir "$WORK/built" "$WORK/tag"
tar -xzf "$WORK/venvy.tar.gz" -C "$WORK/built"
git archive "$TAG" main.py requirements.txt install.sh venvy web | tar -x -C "$WORK/tag"
echo "$TAG" > "$WORK/tag/VERSION"
if ! diff -r "$WORK/tag" "$WORK/built" >"$WORK/diff.txt"; then
  cat "$WORK/diff.txt" >&2
  echo "The release archive is not $TAG as it is here. Not signed." >&2
  exit 1
fi
# (The install.sh beside it is what a first install runs: the tag's too.)
cmp -s "$WORK/install.sh" "$WORK/tag/install.sh" || { echo "The release's install.sh is not $TAG's. Not signed." >&2; exit 1; }

openssl pkeyutl -sign -inkey "$KEY" -rawin -in "$WORK/venvy.tar.gz.sha256" -out "$WORK/venvy.tar.gz.sha256.sig"
openssl pkey -in "$KEY" -pubout > "$WORK/pub.pem"
openssl pkeyutl -verify -pubin -inkey "$WORK/pub.pem" -rawin -in "$WORK/venvy.tar.gz.sha256" \
  -sigfile "$WORK/venvy.tar.gz.sha256.sig" >/dev/null
gh release upload "$TAG" "$WORK/venvy.tar.gz.sha256.sig" --clobber
gh release edit "$TAG" --draft=false --latest
echo "$TAG is signed and published."
