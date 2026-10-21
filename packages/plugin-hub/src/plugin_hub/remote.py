"""Reading other repos: their marketplace.json, and their plugin release tags.

Releases are read from tags, never from a stored list, so a release in any
source repo is visible the moment its tag is pushed. Tags are listed with
`git ls-remote`, which needs no API token and has no rate limit for public repos.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.request
from pathlib import Path

VERSION = re.compile(r"^\d+(\.\d+)*$")


class RemoteError(Exception):
    pass


def read_json(location: str) -> dict:
    """A JSON document from an https URL or a local path."""
    if location.startswith(("https://", "http://")):
        try:
            with urllib.request.urlopen(location, timeout=30) as r:
                return json.load(r)
        except OSError as e:
            raise RemoteError(f"cannot fetch {location}: {e}") from e
    try:
        return json.loads(Path(location).read_text())
    except OSError as e:
        raise RemoteError(f"cannot read {location}: {e}") from e


def semver(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split("."))


def tag_for(plugin: str, version: str) -> str:
    """The tag `claude plugin tag` writes for a plugin release."""
    return f"{plugin}--v{version}"


def git_url(source: dict) -> str | None:
    """The git URL a catalogue source fetches from, or None for a non-git source."""
    if source.get("source") == "github":
        return f"https://github.com/{source['repo']}.git"
    if source.get("source") in ("url", "git-subdir"):
        return source["url"]
    return None


def manifest(source: dict, ref: str) -> dict | None:
    """A plugin's plugin.json at `ref`, or None where it cannot be read cheaply.

    Only GitHub-hosted sources are read, through raw.githubusercontent.com: the
    one host that serves a single file at a ref without a clone.
    """
    url = git_url(source) or ""
    if not url.startswith("https://github.com/"):
        return None
    repo = url.removeprefix("https://github.com/").removesuffix(".git")
    path = source.get("path", ".").strip("/")
    try:
        return read_json(f"https://raw.githubusercontent.com/{repo}/{ref}/{path}/"
                         ".claude-plugin/plugin.json")
    except RemoteError:
        return None


def versions(url: str, plugin: str) -> list[str]:
    """Released versions of `plugin` in the repo at `url`, newest first."""
    out = subprocess.run(
        ["git", "ls-remote", "--tags", "--refs", url, f"refs/tags/{plugin}--v*"],
        capture_output=True, text=True)
    if out.returncode != 0:
        raise RemoteError(f"cannot list tags of {url}: {out.stderr.strip()}")
    prefix = f"refs/tags/{plugin}--v"
    found = [line.split("\t", 1)[1][len(prefix):] for line in out.stdout.splitlines()]
    # A pre-release or a malformed tag is not offered; pin it by `ref` if wanted.
    return sorted((v for v in found if VERSION.match(v)), key=semver, reverse=True)
