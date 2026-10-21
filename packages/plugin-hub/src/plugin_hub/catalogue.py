"""The catalogue: one marketplace listing every plugin in every source repo.

Each source repo keeps its own .claude-plugin/marketplace.json; the catalogue
is built from those, so nothing about a plugin is written down twice. A plugin
a source lists by relative path becomes a `git-subdir` entry pointing back into
that repo. Entries carry no version and no ref: the catalogue says what exists
and where, and releases are read from tags when they are needed.

Registered at user scope, it is what `/plugin` browses. Projects do not install
from it -- they pin from it, into a marketplace of their own (see project.py).
"""

from __future__ import annotations

import posixpath
import tomllib
from collections.abc import Callable
from pathlib import Path

from .remote import RemoteError

NAME = "plugin-hub"
DEFAULT_LOCATION = ("https://raw.githubusercontent.com/ubyndr/plugin-hub/main/"
                    ".claude-plugin/marketplace.json")
# Plugin manifest fields worth showing when browsing; the rest stay in the source.
CARRIED = ("description", "version", "author", "homepage", "license", "keywords", "category")


class CatalogueError(Exception):
    pass


def read_sources(path: Path) -> list[dict]:
    sources = tomllib.loads(path.read_text()).get("source", [])
    for s in sources:
        if "repo" not in s:
            raise CatalogueError(f"{path}: every [[source]] needs a repo = \"owner/name\"")
    return sources


def marketplace_url(source: dict) -> str:
    ref = source.get("ref", "HEAD")
    return (f"https://raw.githubusercontent.com/{source['repo']}/{ref}/"
            ".claude-plugin/marketplace.json")


def entry_source(repo: str, marketplace: dict, source: str | dict) -> dict:
    """A plugin's source as seen from outside its repo."""
    if isinstance(source, dict):
        return dict(source)
    root = marketplace.get("metadata", {}).get("pluginRoot", "")
    rel = source if source.startswith("./") else posixpath.join(root or ".", source)
    return {"source": "git-subdir", "url": f"https://github.com/{repo}.git",
            "path": posixpath.normpath(rel)}


def build(sources: list[dict], fetch: Callable[[str], dict]) -> dict:
    plugins: dict[str, dict] = {}
    origin: dict[str, str] = {}
    for src in sources:
        repo = src["repo"]
        try:
            market = fetch(marketplace_url(src))
        except RemoteError as e:
            raise CatalogueError(str(e)) from e
        for p in market.get("plugins", []):
            name = p["name"]
            if name in plugins:
                raise CatalogueError(
                    f"plugin {name!r} is in both {origin[name]} and {repo}; plugin names "
                    f"must be unique across the catalogue, so rename one at its source")
            entry = {"name": name, "source": entry_source(repo, market, p["source"])}
            entry.update({k: p[k] for k in CARRIED if k in p and k != "version"})
            entry.setdefault("homepage", f"https://github.com/{repo}")
            plugins[name] = entry
            origin[name] = repo
    return {
        "name": NAME,
        "owner": {"name": "Cellular Semantics", "url": "https://github.com/Cellular-Semantics"},
        "metadata": {
            "description": "Every Claude Code plugin Cellular Semantics manages, across "
                           "all of its plugin repos. Browse here; pin per project with "
                           "plugin-hub. Generated from sources.toml; do not edit.",
        },
        "plugins": [plugins[n] for n in sorted(plugins)],
    }


def load(location: str, fetch: Callable[[str], dict]) -> dict[str, dict]:
    """Catalogue entries by plugin name."""
    try:
        market = fetch(location)
    except RemoteError as e:
        raise CatalogueError(f"{e}\nSet PLUGIN_HUB_CATALOGUE to a catalogue URL or path.") from e
    return {p["name"]: p for p in market.get("plugins", [])}
