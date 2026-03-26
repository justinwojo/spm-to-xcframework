# Feature Backlog

## Binary mode (`--binary`)

Support downloading pre-built xcframeworks distributed via SPM binary targets, without building from source.

**Why:** Many libraries (BlinkID, Firebase, etc.) distribute pre-built xcframeworks through SPM binary targets. Today the tool only handles source builds, so users of these libraries can't use it at all.

**How it would work:**

```bash
spm-to-xcframework https://github.com/BlinkID/blinkid-swift-package.git -v 7.6.2 --binary
```

1. Create a temporary `Package.swift` that depends on the target repo
2. Run `swift package resolve` to download binary artifacts
3. Locate xcframeworks in `.build/artifacts/`
4. Copy to the output directory

**Edge cases:**
- Multiple xcframeworks with the same name in different artifact paths — may need an `--artifact-path` override
- Some binary packages have both binary targets and source targets — `--binary` should only resolve the binary ones
- Validate the downloaded xcframework the same way source-built ones are validated

## Revision verification (`--revision`)

Verify that the git tag resolves to an expected commit SHA before building.

**Why:** Supply-chain security. Users pinning a version want assurance that the tag hasn't been force-pushed to a different commit since they last verified it. This is especially important for CI pipelines where builds should be reproducible and tamper-evident.

**How it would work:**

```bash
spm-to-xcframework https://github.com/kean/Nuke.git -v 12.8.0 --revision abc123...
```

1. Before cloning, run `git ls-remote --tags <url> refs/tags/<tag> refs/tags/<tag>^{}`
2. Compare the resolved SHA against the provided `--revision` value
3. If mismatch, fail with a clear error showing expected vs actual SHA
4. If match, proceed with the build as normal

**Notes:**
- Should accept full 40-character SHA only (no short SHAs)
- The `^{}` dereferenced tag lookup handles annotated tags correctly
- This is a pre-flight check — no source is fetched until verification passes

## Multi-product filter (`--product` repeated)

Allow filtering to multiple products without building everything.

**Why:** For large packages (e.g., Stripe with 10+ products), building everything when you only need 2-3 is wasteful. Today `--product` only accepts a single name, so users must either build one at a time (re-cloning each time) or build all.

**How it would work:**

```bash
spm-to-xcframework https://github.com/stripe/stripe-ios.git -v 25.6.2 \
  --product StripeCore --product StripePayments
```

The `--product` flag would be repeatable, collecting into a list. Product discovery and scheme matching would proceed as normal, but only the listed products would be built. If any listed product doesn't exist, fail with an error.
