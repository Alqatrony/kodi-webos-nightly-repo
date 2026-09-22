#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

MIRROR = "https://mirrors.kodi.tv/nightlies/webos/master/"
KODI_ID = "org.xbmc.kodi"
ICON_URL = "https://raw.githubusercontent.com/xbmc/xbmc/master/tools/webOS/packaging/largeIcon.png"
KEEP_RELEASES = int(os.environ.get("KEEP_RELEASES", "3"))
WORKSPACE = Path(os.environ.get("GITHUB_WORKSPACE", ".")).resolve()
REPOSITORY = os.environ["GITHUB_REPOSITORY"]

NIGHTLY_RE = re.compile(
    r"org\.xbmc\.kodi_(\d{8})-([0-9a-fA-F]+)-master_arm\.ipk"
)

def run(args, cwd=None, capture=False):
    proc = subprocess.run(
        args,
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )
    return proc.stdout.strip() if capture else ""

def fetch_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": "kodi-webos-nightly-repo/1.0"})
    with urllib.request.urlopen(req, timeout=60) as response:
        return response.read().decode("utf-8", errors="replace")

def download(url, destination):
    req = urllib.request.Request(url, headers={"User-Agent": "kodi-webos-nightly-repo/1.0"})
    with urllib.request.urlopen(req, timeout=180) as response, destination.open("wb") as out:
        shutil.copyfileobj(response, out, length=1024 * 1024)

def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def discover_latest():
    html = fetch_text(MIRROR)
    found = {(m.group(1), m.group(2).lower(), m.group(0)) for m in NIGHTLY_RE.finditer(html)}
    if not found:
        raise RuntimeError("No Kodi webOS master nightly IPK found in the official mirror.")
    return sorted(found, key=lambda x: (x[0], x[1]), reverse=True)[0]

def extract_ipk(ipk, destination):
    destination.mkdir(parents=True, exist_ok=True)
    run(["ar", "x", str(ipk)], cwd=destination)
    members = [p.name for p in destination.iterdir() if p.is_file()]
    data_name = next((n for n in members if n.startswith("data.tar")), None)
    control_name = next((n for n in members if n.startswith("control.tar")), None)
    if not data_name or not control_name:
        raise RuntimeError(f"Unexpected IPK layout: {members}")
    return destination / data_name, destination / control_name

def unpack_tar(archive, destination):
    destination.mkdir(parents=True, exist_ok=True)
    run(["tar", "-xf", str(archive), "-C", str(destination)])

def repack_tar(archive, source_dir):
    archive.unlink(missing_ok=True)
    run([
        "tar", "--sort=name", "--owner=0", "--group=0", "--numeric-owner",
        "-caf", str(archive), "-C", str(source_dir), "."
    ])

def find_appinfo(data_root):
    expected = data_root / "usr/palm/applications/org.xbmc.kodi/appinfo.json"
    if expected.exists():
        return expected
    for path in data_root.rglob("appinfo.json"):
        if KODI_ID in str(path):
            return path
    raise RuntimeError("Kodi appinfo.json was not found inside the IPK.")

def make_tracking_version(date, previous_state):
    year = int(date[2:4])
    month = int(date[4:6])
    day = int(date[6:8])

    revision = 0
    if previous_state.get("sourceDate") == date:
        revision = int(previous_state.get("sameDayRevision", 0)) + 1

    if revision > 9:
        raise RuntimeError("More than 10 same-day builds are not supported.")

    return f"{year}.{month}.{day * 10 + revision}", revision

def patch_metadata(data_root, control_root, tracking_version):
    appinfo_path = find_appinfo(data_root)
    appinfo = json.loads(appinfo_path.read_text(encoding="utf-8"))
    original_version = str(appinfo.get("version", "unknown"))
    appinfo["version"] = tracking_version
    appinfo_path.write_text(
        json.dumps(appinfo, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8"
    )

    controls = [p for p in control_root.rglob("control") if p.is_file()]
    if controls:
        control_path = controls[0]
        text = control_path.read_text(encoding="utf-8")
        if re.search(r"(?m)^Version:\s*.+$", text):
            text = re.sub(
                r"(?m)^Version:\s*.+$",
                f"Version: {tracking_version}",
                text,
                count=1
            )
            control_path.write_text(text, encoding="utf-8")

    return original_version

def rebuild_ipk(source_ipk, ar_root, data_archive, control_archive, output_ipk):
    shutil.copy2(source_ipk, output_ipk)
    run(["ar", "r", str(output_ipk), control_archive.name, data_archive.name], cwd=ar_root)

def verify_ipk(ipk, expected_version, root):
    data_archive, _ = extract_ipk(ipk, root / "ar")
    data_root = root / "data"
    unpack_tar(data_archive, data_root)
    appinfo = json.loads(find_appinfo(data_root).read_text(encoding="utf-8"))
    actual = str(appinfo.get("version"))
    if actual != expected_version:
        raise RuntimeError(
            f"Verification failed: expected app version {expected_version}, got {actual}."
        )

def release_exists(tag):
    proc = subprocess.run(
        ["gh", "release", "view", tag, "--repo", REPOSITORY],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc.returncode == 0

def publish_release(tag, asset, title, notes):
    if release_exists(tag):
        run(["gh", "release", "upload", tag, str(asset), "--clobber", "--repo", REPOSITORY])
    else:
        run([
            "gh", "release", "create", tag, str(asset),
            "--repo", REPOSITORY,
            "--title", title,
            "--notes", notes,
        ])

def cleanup_old_releases():
    raw = run([
        "gh", "release", "list",
        "--repo", REPOSITORY,
        "--limit", "100",
        "--json", "tagName,createdAt",
    ], capture=True)
    releases = json.loads(raw or "[]")
    nightly = [r for r in releases if str(r.get("tagName", "")).startswith("nightly-")]
    nightly.sort(key=lambda r: r.get("createdAt", ""), reverse=True)

    for old in nightly[KEEP_RELEASES:]:
        tag = old["tagName"]
        print(f"Deleting old release {tag}")
        run(["gh", "release", "delete", tag, "--repo", REPOSITORY, "--yes", "--cleanup-tag"])

def write_repo_json(tracking_version, date, commit, release_tag, asset_name, sha256):
    release_url = f"https://github.com/{REPOSITORY}/releases/download/{release_tag}/{asset_name}"
    source_url = f"https://github.com/xbmc/xbmc/commit/{commit}"
    pretty_date = f"{date[0:4]}-{date[4:6]}-{date[6:8]}"

    description = (
        f"Kodi webOS master nightly {pretty_date} ({commit}). "
        "Unofficial metadata-only repack for Homebrew Channel update detection. "
        "Kodi binaries come from Team Kodi's official nightly."
    )

    repo = {
        "paging": {
            "page": 1,
            "count": 1,
            "maxPage": 1,
            "itemsTotal": 1
        },
        "packages": [
            {
                "id": KODI_ID,
                "title": "Kodi Nightly",
                "description": description,
                "iconUri": ICON_URL,
                "manifest": {
                    "id": KODI_ID,
                    "version": tracking_version,
                    "type": "native",
                    "title": "Kodi Nightly",
                    "appDescription": description,
                    "iconUri": ICON_URL,
                    "sourceUrl": source_url,
                    "rootRequired": False,
                    "ipkUrl": release_url,
                    "ipkHash": {"sha256": sha256}
                }
            }
        ]
    }

    (WORKSPACE / "repo.json").write_text(
        json.dumps(repo, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8"
    )

def main():
    date, commit, source_filename = discover_latest()
    source_url = MIRROR + source_filename
    print(f"Latest official nightly: {source_filename}")

    state_path = WORKSPACE / "build-state.json"
    previous_state = {}
    if state_path.exists():
        previous_state = json.loads(state_path.read_text(encoding="utf-8"))

    if previous_state.get("sourceFilename") == source_filename:
        print("Already up to date.")
        return 0

    tracking_version, same_day_revision = make_tracking_version(date, previous_state)
    release_tag = f"nightly-{date}-{commit}"
    asset_name = f"org.xbmc.kodi_{date}-{commit}-homebrew_arm.ipk"

    with tempfile.TemporaryDirectory(prefix="kodi-nightly-") as tmp:
        tmp_root = Path(tmp)
        source_ipk = tmp_root / source_filename

        print(f"Downloading {source_url}")
        download(source_url, source_ipk)
        upstream_sha256 = sha256_file(source_ipk)

        ar_root = tmp_root / "unpacked"
        data_archive, control_archive = extract_ipk(source_ipk, ar_root)

        data_root = tmp_root / "data"
        control_root = tmp_root / "control"
        unpack_tar(data_archive, data_root)
        unpack_tar(control_archive, control_root)

        original_version = patch_metadata(data_root, control_root, tracking_version)

        repack_tar(data_archive, data_root)
        repack_tar(control_archive, control_root)

        output_ipk = tmp_root / asset_name
        rebuild_ipk(source_ipk, ar_root, data_archive, control_archive, output_ipk)

        verify_ipk(output_ipk, tracking_version, tmp_root / "verify")
        output_sha256 = sha256_file(output_ipk)

        pretty_date = f"{date[0:4]}-{date[4:6]}-{date[6:8]}"
        notes = (
            f"Automated Kodi webOS master nightly for {pretty_date}.\n\n"
            f"Upstream nightly: {source_url}\n"
            f"Upstream commit: https://github.com/xbmc/xbmc/commit/{commit}\n"
            f"Upstream IPK SHA-256: {upstream_sha256}\n"
            f"Homebrew tracking version: {tracking_version}\n\n"
            "This is an unofficial metadata-only repack. "
            "The automation changes package/app version metadata so Homebrew Channel "
            "can distinguish consecutive nightly builds."
        )

        publish_release(
            release_tag,
            output_ipk,
            f"Kodi webOS Nightly {pretty_date} ({commit})",
            notes
        )

        write_repo_json(
            tracking_version,
            date,
            commit,
            release_tag,
            asset_name,
            output_sha256
        )

        state = {
            "sourceFilename": source_filename,
            "sourceUrl": source_url,
            "sourceDate": date,
            "sourceCommit": commit,
            "sameDayRevision": same_day_revision,
            "originalVersion": original_version,
            "trackingVersion": tracking_version,
            "upstreamSha256": upstream_sha256,
            "repackedSha256": output_sha256,
            "releaseTag": release_tag,
            "assetName": asset_name,
            "updatedAt": datetime.now(timezone.utc).isoformat()
        }
        state_path.write_text(
            json.dumps(state, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8"
        )

    cleanup_old_releases()
    print("Repository metadata updated successfully.")
    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
