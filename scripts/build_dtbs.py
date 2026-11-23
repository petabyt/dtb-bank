#!/usr/bin/env python3
"""Build configured kernel DTBs and publish missing GitHub releases."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

import yaml


ARCHES = {"arm": "arm-linux-gnueabihf-", "arm64": "aarch64-linux-gnu-"}
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
GITHUB_REPO = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")


def run(*args, cwd=None, capture=False):
    print("+", " ".join(map(str, args)), flush=True)
    return subprocess.run(args, cwd=cwd, check=True, text=True,
                          stdout=subprocess.PIPE if capture else None).stdout


def validate(config):
    if not isinstance(config, dict) or not isinstance(config.get("kernels"), list):
        raise ValueError("kernels must be a list")
    releases = set()
    for kernel in config["kernels"]:
        if not isinstance(kernel, dict):
            raise ValueError("each kernel must be a mapping")
        name = kernel.get("name", "")
        if not isinstance(name, str) or not SAFE_NAME.fullmatch(name):
            raise ValueError(f"invalid kernel name: {name!r}")
        if not isinstance(kernel.get("repository"), str) or not GITHUB_REPO.fullmatch(kernel["repository"]):
            raise ValueError(f"{name}: repository must be a GitHub HTTPS URL")
        if kernel.get("selection") not in ("all", "listed"):
            raise ValueError(f"{name}: selection must be all or listed")
        versions = kernel.get("versions")
        if not isinstance(versions, list) or not versions:
            raise ValueError(f"{name}: versions must be a nonempty list")
        targets = kernel.get("targets", [])
        if not isinstance(targets, list) or (kernel.get("enabled", True) and kernel["selection"] == "listed" and not targets):
            raise ValueError(f"{name}: listed selection requires targets")
        for target in targets:
            if not isinstance(target, str) or not target.endswith(".dtb") or target.startswith("/") or any(part in ("", ".", "..") for part in target.split("/")):
                raise ValueError(f"{name}: invalid target {target!r}")
        for version in versions:
            if not isinstance(version, str) or not SAFE_NAME.fullmatch(version):
                raise ValueError(f"{name}: invalid version {version!r}")
            release = f"{name}-{version}"
            if release in releases:
                raise ValueError(f"duplicate release tag: {release}")
            releases.add(release)


def resolve_commit(repository, version):
    output = run("git", "ls-remote", repository, f"refs/tags/{version}",
                 f"refs/tags/{version}^{{}}", capture=True)
    refs = {ref: sha for sha, ref in (line.split() for line in output.splitlines())}
    tag = f"refs/tags/{version}"
    commit = refs.get(tag + "^{}", refs.get(tag))
    if not commit:
        raise ValueError(f"tag {version} not found in {repository}")
    return commit


def download_source(repository, commit, destination):
    owner, repo = GITHUB_REPO.fullmatch(repository).groups()
    url = f"https://github.com/{owner}/{repo}/archive/{commit}.tar.gz"
    archive = destination / "source.tar.gz"
    print(f"Downloading {url}", flush=True)
    with urllib.request.urlopen(url, timeout=120) as response, archive.open("wb") as output:
        shutil.copyfileobj(response, output)
    with tarfile.open(archive, "r:gz") as tar:
        roots = {Path(member.name).parts[0] for member in tar.getmembers() if member.name}
        if len(roots) != 1:
            raise ValueError("source archive does not have exactly one root directory")
        tar.extractall(destination, filter="data")
    archive.unlink()
    return destination / roots.pop()


def release_exists(tag):
    result = subprocess.run(["gh", "api", f"repos/{os.environ['GH_REPO']}/git/ref/tags/{tag}"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if result.returncode == 0:
        release = subprocess.run(["gh", "release", "view", tag, "--json", "isDraft"],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if release.returncode or json.loads(release.stdout)["isDraft"]:
            raise RuntimeError(f"tag {tag} exists without a published release; inspect it before retrying")
        return True
    if "HTTP 404" not in result.stderr:
        raise RuntimeError(f"could not check tag {tag}: {result.stderr.strip()}")
    return False


def build(kernel, version, source, output):
    targets = {arch: [] for arch in ARCHES}
    if kernel["selection"] == "listed":
        for target in kernel["targets"]:
            matches = [arch for arch in ARCHES if (source / "arch" / arch / "boot/dts" / target.replace(".dtb", ".dts")).is_file()]
            if len(matches) != 1:
                raise ValueError(f"{target}: expected exactly one source architecture, found {matches}")
            targets[matches[0]].append(target)
    else:
        targets = {arch: ["dtbs"] for arch in ARCHES}

    assets = []
    paths = {}
    for arch, requested in targets.items():
        if not requested:
            continue
        build_dir = output / f"build-{arch}"
        build_dir.mkdir()
        common = ["make", f"O={build_dir}", f"ARCH={arch}", f"CROSS_COMPILE={ARCHES[arch]}"]
        run(*common, "defconfig", cwd=source)
        run(*common, f"-j{os.cpu_count() or 2}", *requested, cwd=source)
        dts_dir = build_dir / "arch" / arch / "boot/dts"
        if kernel["selection"] == "listed":
            dtbs = [dts_dir / target for target in requested]
        else:
            dtbs = [p for p in dts_dir.rglob("*.dtb") if "rockchip" in p.parts or "allwinner" in p.parts]
        if not dtbs or any(not p.is_file() for p in dtbs):
            raise RuntimeError(f"{version}: missing DTB output for {arch}")
        for dtb in sorted(dtbs):
            relative = dtb.relative_to(dts_dir).as_posix()
            asset_name = f"{arch}-{relative.replace('/', '-')}"
            if asset_name in paths:
                raise ValueError(f"asset name collision: {asset_name}")
            paths[asset_name] = f"arch/{arch}/boot/dts/{relative}"
            asset = output / asset_name
            shutil.copyfile(dtb, asset)
            assets.append(asset)
    return assets, paths


def publish(kernel, version):
    tag = f"{kernel['name']}-{version}"
    if release_exists(tag):
        print(f"Skipping existing release {tag}", flush=True)
        return
    commit = resolve_commit(kernel["repository"], version)
    with tempfile.TemporaryDirectory(prefix="dtb-bank-") as tmp:
        work = Path(tmp)
        source = download_source(kernel["repository"], commit, work)
        assets, paths = build(kernel, version, source, work)
        metadata = {"repository": kernel["repository"], "ref": version, "commit": commit,
                    "selection": kernel["selection"], "targets": kernel.get("targets", []),
                    "asset_source_paths": paths, "configuration": "defconfig",
                    "compiler_versions": {arch: run(prefix + "gcc", "--version", capture=True).splitlines()[0] for arch, prefix in ARCHES.items()},
                    "build_commands": "make O=<build-dir> ARCH=<arch> CROSS_COMPILE=<prefix> defconfig; make O=<build-dir> ARCH=<arch> CROSS_COMPILE=<prefix> <targets>"}
        meta = work / "build-metadata.json"
        meta.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
        checksums = []
        for algorithm, filename in (("md5", "MD5SUMS"), ("sha256", "SHA256SUMS")):
            checksum = work / filename
            checksum.write_text("".join(f"{hashlib.new(algorithm, asset.read_bytes()).hexdigest()}  {asset.name}\n" for asset in sorted(assets)))
            checksums.append(checksum)
        run("gh", "release", "create", tag, "--draft", "--title", tag,
            "--notes", f"DTBs built from {kernel['repository']} at {version} ({commit}).")
        for asset in [*assets, meta, *checksums]:
            run("gh", "release", "upload", tag, str(asset))
        run("gh", "release", "edit", tag, "--draft=false")


def main():
    if len(sys.argv) != 2:
        raise ValueError("usage: build_dtbs.py kernels.yaml")
    with open(sys.argv[1], encoding="utf-8") as file:
        config = yaml.safe_load(file)
    validate(config)
    failures = []
    for kernel in config["kernels"]:
        if not kernel.get("enabled", True):
            print(f"Skipping disabled kernel {kernel['name']}", flush=True)
            continue
        for version in kernel["versions"]:
            try:
                publish(kernel, version)
            except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
                print(f"Failed {kernel['name']}-{version}: {error}", file=sys.stderr, flush=True)
                failures.append(f"{kernel['name']}-{version}")
    if failures:
        raise SystemExit("Failed releases: " + ", ".join(failures))


if __name__ == "__main__":
    main()
