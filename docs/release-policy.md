# Release and version policy

Bitcast X software releases use Semantic Versioning. The first public software release from this
repository is `2.0.0`.

The software version is independent of the product/repository name and the protocol boundary
versions listed in [protocol.md](protocol.md). A protocol change follows the separate compatibility
rules in [protocol-compatibility.md](protocol-compatibility.md) and may or may not coincide with a
software major release.

## Version changes

- **Major**: incompatible operator, package, or supported-state behavior.
- **Minor**: backward-compatible operator or application functionality.
- **Patch**: backward-compatible fixes and documentation corrections.

Every release must update `pyproject.toml`, `bitcast_x.__version__`, the container version default,
the CI image version, and `CHANGELOG.md` together. Regression tests enforce equality across those
surfaces. Validator remote-log labels and public health responses use `bitcast_x.__version__`.

## Immutable release identity

1. Complete the repository's quality, container, operator, security, and compatibility gates.
2. Merge the reviewed release commit to `main`.
3. Create an annotated `vX.Y.Z` tag pointing to that exact commit.
4. Build source/wheel artifacts and container images from that tag with no uncommitted changes.
5. Supply the full source commit SHA as the OCI `org.opencontainers.image.revision` label and the
   release number as `org.opencontainers.image.version`.
6. Publish image tags for both `vX.Y.Z` and the full commit SHA, and record the immutable digest in
   the release notes.

Operators should pin an annotated release tag or immutable container digest. Automatic source
updates remain opt-in and are not a substitute for an immutable release.
