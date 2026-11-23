# DTB bank

The workflow builds Rockchip and Allwinner device trees from the kernel tags in
`kernels.yaml` and publishes each DTB as an individual GitHub release asset.
It runs on a schedule, when its configuration changes, or manually through
GitHub Actions. Give Actions permission to create repository contents and
releases.

Each kernel entry names a GitHub repository and one or more exact source tags.
For `selection: all`, both ARM and ARM64 DTBs are built and the Rockchip and
Allwinner outputs are collected. For `selection: listed`, `targets` contains
paths relative to `arch/<arch>/boot/dts/`, with `.dtb` extensions. The script
infers the architecture from the corresponding `.dts` source and rejects
ambiguous or missing targets. Disabled entries are skipped.

Release tags are `<name>-<version>`. Asset names start with the architecture,
followed by the DTB path with directory separators replaced by hyphens. Each
release also contains `MD5SUMS`, `SHA256SUMS`, and `build-metadata.json`.
Checksums cover the DTB assets; MD5 is provided for compatibility, not security.
Mainline builds do not include Armbian patches. Vendor builds use the configured
repository without additional patches.

Existing published releases are skipped. Uploads begin as drafts, and a tag
with a draft or missing release is treated as an error rather than silently
replaced. Inspect and remove the incomplete release and tag manually before
retrying. Changing a published configuration entry does not rebuild it; use a
new release tag for a revised build.

To run locally, install the dependencies in `requirements.txt`, the ARM and
ARM64 cross-compilers and kernel build dependencies, and authenticate the GitHub
CLI with release write access. Set `GH_REPO` to the destination repository and
run `python scripts/build_dtbs.py kernels.yaml`.
