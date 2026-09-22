# Kodi webOS Nightly Homebrew Repository

A small Homebrew Channel repository that tracks Team Kodi's official webOS `master` nightly builds.

## Repository URL

```text
https://raw.githubusercontent.com/Alqatrony/kodi-webos-nightly-repo/main/repo.json
```

## How it works

1. GitHub Actions checks Team Kodi's official webOS nightly directory every 6 hours.
2. If a new nightly is found, the official IPK is downloaded.
3. Only package/app version metadata is changed so Homebrew Channel can tell nightly builds apart.
4. The repacked IPK is published as a GitHub Release.
5. `repo.json` is regenerated with the new version, download URL, and SHA-256 hash.
6. Only the latest 3 generated releases are retained.

## Important

This repository is unofficial and is not affiliated with Team Kodi or webOS Homebrew Channel.
Kodi binaries originate from Team Kodi's official nightly IPK.
