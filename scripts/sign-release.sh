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
command -v gh >/dev/null 2>&1 || { echo "gh CLI is required." >&2; exit 1; }
command -v openssl >/dev/null 2>&1 || { echo "openssl is required." >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "python3 is required." >&2; exit 1; }
[ -r "$KEY" ] || { echo "No release key at $KEY." >&2; exit 1; }
case "$TAG" in ''|.|..|*[!0-9A-Za-z._-]*) echo "Invalid tag name: $TAG" >&2; exit 1 ;; esac

# Require the tag to exist locally — do not blindly fetch/trust remote tags.
git rev-parse -q --verify "refs/tags/$TAG" >/dev/null || { echo "No local tag $TAG (fetch/verify it first)." >&2; exit 1; }

# The key must be the one the tagged install.sh trusts.
want="$(git show "$TAG:install.sh" | sed -n "/^RELEASE_PUBKEY='/,/END PUBLIC KEY/p" | sed "s/^RELEASE_PUBKEY='//; s/'$//")"
have="$(openssl pkey -in "$KEY" -pubout)"
[ -n "$want" ] && [ "$want" = "$have" ] || { echo "$KEY is not the key in $TAG:install.sh (RELEASE_PUBKEY)." >&2; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
gh release download "$TAG" -p venvy.tar.gz -p venvy.tar.gz.sha256 -p install.sh -p VERSION -p CHANGELOG.md -D "$WORK"

# Validate checksum format strictly and check archive hash (Linux sha256sum or macOS shasum).
[ "$(wc -l < "$WORK/venvy.tar.gz.sha256")" -eq 1 ] && grep -Eq '^[0-9a-f]{64}  venvy\.tar\.gz$' "$WORK/venvy.tar.gz.sha256" \
  || { echo "Unexpected checksum file format." >&2; exit 1; }
if command -v sha256sum >/dev/null 2>&1; then
  ( cd "$WORK" && sha256sum -c --status venvy.tar.gz.sha256 ) || { echo "The archive doesn't match its checksum." >&2; exit 1; }
else
  ( cd "$WORK" && shasum -a 256 -c -s venvy.tar.gz.sha256 ) || { echo "The archive doesn't match its checksum." >&2; exit 1; }
fi

# Compare the workflow tarball directly against the local git tag without
# extracting untrusted tar entries onto disk (rejects symlinks/special files).
git archive --format=tar "$TAG" main.py requirements.txt requirements.lock install.sh venvy web > "$WORK/tag.tar"
python3 - "$WORK/venvy.tar.gz" "$WORK/tag.tar" "$TAG" <<'PY'
import posixpath, sys, tarfile

built_path, tag_path, tag = sys.argv[1], sys.argv[2], sys.argv[3]

def read_tar(path, mode):
    files = {}
    with tarfile.open(path, mode) as tf:
        for m in tf.getmembers():
            name = posixpath.normpath(m.name)
            if name.startswith("/") or name == ".." or name.startswith("../"):
                sys.exit(f"Unsafe archive path in {path}: {m.name!r}")
            if m.isdir():
                continue
            if not m.isreg():
                sys.exit(f"Non-regular archive member in {path}: {m.name!r} (type={m.type!r})")
            if name in files:
                sys.exit(f"Duplicate archive member in {path}: {name!r}")
            f = tf.extractfile(m)
            files[name] = (f.read() if f else b"", m.mode & 0o111)
    return files

built = read_tar(built_path, "r:gz")
expected = read_tar(tag_path, "r:")
expected["VERSION"] = ((tag + "\n").encode("utf-8"), 0)

extra = sorted(set(built) - set(expected))
missing = sorted(set(expected) - set(built))
if extra or missing:
    if missing:
        print("Missing from built archive:", ", ".join(missing), file=sys.stderr)
    if extra:
        print("Unexpected in built archive:", ", ".join(extra), file=sys.stderr)
    sys.exit("The release archive does not match the local tag. Not signed.")

for name in sorted(expected):
    b_data, b_exec = built[name]
    e_data, e_exec = expected[name]
    if b_data != e_data:
        sys.exit(f"Content mismatch for {name}. Not signed.")
    if name != "VERSION" and bool(b_exec) != bool(e_exec):
        sys.exit(f"Executable bit mismatch for {name}. Not signed.")
PY

# Verify the standalone assets beside venvy.tar.gz match the local tag too.
git show "$TAG:install.sh" > "$WORK/tag-install.sh"
git show "$TAG:CHANGELOG.md" > "$WORK/tag-CHANGELOG.md"
printf '%s\n' "$TAG" > "$WORK/tag-VERSION"
cmp -s "$WORK/install.sh" "$WORK/tag-install.sh" || { echo "The release's install.sh is not $TAG's. Not signed." >&2; exit 1; }
cmp -s "$WORK/VERSION" "$WORK/tag-VERSION" || { echo "The release's VERSION is not $TAG. Not signed." >&2; exit 1; }
cmp -s "$WORK/CHANGELOG.md" "$WORK/tag-CHANGELOG.md" || { echo "The release's CHANGELOG.md is not $TAG's. Not signed." >&2; exit 1; }

openssl pkeyutl -sign -inkey "$KEY" -rawin -in "$WORK/venvy.tar.gz.sha256" -out "$WORK/venvy.tar.gz.sha256.sig"
openssl pkey -in "$KEY" -pubout > "$WORK/pub.pem"
openssl pkeyutl -verify -pubin -inkey "$WORK/pub.pem" -rawin -in "$WORK/venvy.tar.gz.sha256" \
  -sigfile "$WORK/venvy.tar.gz.sha256.sig" >/dev/null
gh release upload "$TAG" "$WORK/venvy.tar.gz.sha256.sig" --clobber
gh release edit "$TAG" --draft=false --latest
echo "$TAG is signed and published."
