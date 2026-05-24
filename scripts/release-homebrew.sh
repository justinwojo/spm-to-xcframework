#!/usr/bin/env bash
#
# Update the Homebrew tap to a tagged release of spm-to-xcframework.
#
# Prerequisite: the version tag already exists on origin. Cut it first:
#     git tag -a v0.2.0 -m "..." && git push origin v0.2.0
#
# Then point the tap at it:
#     scripts/release-homebrew.sh v0.2.0
#
# This computes the GitHub source-tarball checksum, rewrites the formula's
# `url` and `sha256`, and commits + pushes the tap. Users pick up the new
# version on `brew update && brew upgrade`.
#
# Override the tap checkout location with TAP_DIR (default: ../homebrew-spm-to-xcframework).
set -euo pipefail

VERSION="${1:?usage: release-homebrew.sh <version>   e.g. v0.2.0}"
REPO="justinwojo/spm-to-xcframework"
TAP_DIR="${TAP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/../homebrew-spm-to-xcframework}"
FORMULA="${TAP_DIR}/Formula/spm-to-xcframework.rb"
TARBALL="https://github.com/${REPO}/archive/refs/tags/${VERSION}.tar.gz"

[ -f "$FORMULA" ] || { echo "error: formula not found at $FORMULA (set TAP_DIR?)" >&2; exit 1; }

echo "==> Fetching ${TARBALL}"
SHA="$(curl -fsSL "$TARBALL" | shasum -a 256 | awk '{print $1}')"
[ -n "$SHA" ] || { echo "error: could not compute sha256 (is tag ${VERSION} pushed?)" >&2; exit 1; }
echo "    sha256 = ${SHA}"

# Rewrite url (any prior tag) and sha256 in place.
sed -i.bak -E "s|archive/refs/tags/[^\"]+|archive/refs/tags/${VERSION}.tar.gz|" "$FORMULA"
sed -i.bak -E "s|sha256 \"[a-f0-9]{64}\"|sha256 \"${SHA}\"|" "$FORMULA"
rm -f "${FORMULA}.bak"

echo "==> Updated formula:"
grep -E '  (url|sha256) ' "$FORMULA"

( cd "$TAP_DIR"
  git add Formula/spm-to-xcframework.rb
  git commit -m "spm-to-xcframework ${VERSION}"
  git push )

echo "==> Done. Verify with: brew update && brew upgrade spm-to-xcframework"
