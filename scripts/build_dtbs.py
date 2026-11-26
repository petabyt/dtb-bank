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
import time
import urllib.error
import urllib.parse
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
        commit = kernel.get("commit")
        if commit is not None and (not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit) or len(versions) != 1):
            raise ValueError(f"{name}: commit must be a full lowercase SHA-1 and requires exactly one version")
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
    with urllib.request.urlopen(url, timeout=600) as response, archive.open("wb") as output:
        shutil.copyfileobj(response, output)
    with tarfile.open(archive, "r:gz") as tar:
        roots = {Path(member.name).parts[0] for member in tar.getmembers() if member.name}
        if len(roots) != 1:
            raise ValueError("source archive does not have exactly one root directory")
        tar.extractall(destination, filter="data")
    archive.unlink()
    return destination / roots.pop()


def github_request(method, url, data=None, content_type="application/json", allow_not_found=False):
    headers = {"Authorization": f"Bearer {os.environ['GH_TOKEN']}",
               "Accept": "application/vnd.github+json", "Content-Type": content_type,
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "dtb-bank"}
    for attempt in range(8):
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            if error.code == 404 and allow_not_found:
                return None
            limited = error.code in (403, 429) and (
                error.headers.get("Retry-After") or error.headers.get("X-RateLimit-Remaining") == "0"
                or "rate limit" in body.lower())
            if not limited or attempt == 7:
                raise RuntimeError(f"GitHub {method} {url}: HTTP {error.code}: {body}") from error
            if error.headers.get("Retry-After"):
                delay = int(error.headers["Retry-After"]) + 1
            elif error.headers.get("X-RateLimit-Remaining") == "0" and error.headers.get("X-RateLimit-Reset"):
                delay = max(1, int(error.headers["X-RateLimit-Reset"]) - int(time.time()) + 5)
            else:
                delay = 60 * (2 ** attempt)
            print(f"GitHub rate limit reached; waiting {delay} seconds", flush=True)
            time.sleep(delay)


def api_url(path):
    return f"https://api.github.com/repos/{os.environ['GH_REPO']}/{path}"


def find_release(tag):
    # List releases rather than looking up a tag: draft releases may not yet
    # have a corresponding Git tag, and need to be resumed on the next run.
    page = 1
    while True:
        releases = github_request("GET", api_url(f"releases?per_page=100&page={page}"))
        for release in releases:
            if release["tag_name"] == tag:
                return release
        if len(releases) < 100:
            return None
        page += 1


def release_assets(release_id):
    assets = []
    page = 1
    while True:
        batch = github_request("GET", api_url(f"releases/{release_id}/assets?per_page=100&page={page}"))
        assets.extend(batch)
        if len(batch) < 100:
            return {asset["name"]: asset for asset in assets}
        page += 1


def upload_release_assets(release, assets):
    if len(assets) > 1000:
        raise ValueError(f"{len(assets)} assets exceed GitHub's limit of 1000 per release")
    existing = release_assets(release["id"])
    names = {asset.name for asset in assets}
    unexpected = set(existing) - names
    if unexpected:
        raise RuntimeError(f"draft contains unexpected assets: {', '.join(sorted(unexpected))}")
    upload_url = release["upload_url"].split("{", 1)[0]
    for asset in assets:
        data = asset.read_bytes()
        if asset.name in existing:
            remote = existing[asset.name]
            digest = "sha256:" + hashlib.sha256(data).hexdigest()
            if remote.get("state") != "uploaded" or remote.get("digest") != digest:
                raise RuntimeError(f"existing draft asset {asset.name} differs or has no SHA-256 digest; inspect the draft")
            continue
        print(f"Uploading {asset.name}", flush=True)
        github_request("POST", upload_url + "?" + urllib.parse.urlencode({"name": asset.name}),
                       data, "application/octet-stream")


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
    release = find_release(tag)
    if release and not release["draft"]:
        print(f"Skipping existing release {tag}", flush=True)
        return
    if release is None and github_request("GET", api_url(f"git/ref/tags/{tag}"), allow_not_found=True):
        raise RuntimeError(f"tag {tag} exists without a release; inspect it before retrying")
    commit = kernel.get("commit") or resolve_commit(kernel["repository"], version)
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
        all_assets = [*assets, meta, *checksums]
        if len(all_assets) > 1000:
            raise ValueError(f"{len(all_assets)} assets exceed GitHub's limit of 1000 per release")
        notes = f"DTBs built from {kernel['repository']} at {version} ({commit})."
        if release is None:
            body = {"tag_name": tag, "draft": True, "name": tag,
                    "body": notes}
            release = github_request("POST", api_url("releases"), json.dumps(body).encode())
        else:
            if release.get("body") != notes:
                raise RuntimeError(f"draft {tag} has different source metadata; inspect it before retrying")
            print(f"Resuming draft release {tag}", flush=True)
        upload_release_assets(release, all_assets)
        github_request("PATCH", api_url(f"releases/{release['id']}"), b'{"draft":false}')


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
