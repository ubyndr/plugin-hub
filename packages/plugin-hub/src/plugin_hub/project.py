"""A project's plugins.toml, and the files Claude Code reads that are made from it.

    plugins.toml                                    what you edit
    .claude/plugin-hub/.claude-plugin/marketplace.json
                                                    the project's own marketplace:
                                                    one entry per plugin, pinned
                                                    to its release tag
    .claude/plugin-hub/<project>-set/               a bundle plugin depending on
                                                    every pinned plugin, so one
                                                    install brings in all of them
    .claude/settings.json                           declares the marketplace and
                                                    enables its plugins
    .gitignore                                      gains .claude/settings.local.json,
                                                    where sync writes this machine's
                                                    path

The marketplace is named after the project because Claude Code keeps one
registration per marketplace name per machine: a name shared by two projects
would give both whichever pins registered last. With one name per project,
two projects on one machine can hold different releases of the same plugin.

Pins live on the entries, as git refs, not as version ranges on the bundle's
dependencies: a marketplace is cloned without its tags, so a range has nothing
to resolve against and silently installs the newest release instead.
"""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .remote import tag_for

CONFIG = "plugins.toml"
HUB_DIR = Path(".claude/plugin-hub")
SETTINGS = Path(".claude/settings.json")
LOCAL_SETTINGS = Path(".claude/settings.local.json")
PIN_KEYS = ("version", "ref", "sha")
SOURCE_KEYS = ("repo", "url", "path")


class ProjectError(Exception):
    pass


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if not s:
        raise ProjectError(f"cannot make a project name from {text!r}; set [project] name")
    return s


@dataclass
class Config:
    name: str
    plugins: dict[str, dict]     # name -> {"version": ...} | {"ref"|"sha": ..., source keys}

    @property
    def marketplace(self) -> str:
        return f"{self.name}-plugins"

    @property
    def bundle(self) -> str:
        return f"{self.name}-set"


def load(root: Path) -> Config:
    path = root / CONFIG
    if not path.exists():
        return Config(slug(root.resolve().name), {})
    data = tomllib.loads(path.read_text())
    name = slug(data.get("project", {}).get("name") or root.resolve().name)
    plugins = {}
    for plugin, spec in data.get("plugins", {}).items():
        spec = {"version": spec} if isinstance(spec, str) else dict(spec)
        pins = [k for k in PIN_KEYS if k in spec]
        if len(pins) != 1:
            raise ProjectError(f"{CONFIG}: {plugin} needs exactly one of "
                               f"version, ref or sha; found {pins or 'none'}")
        plugins[plugin] = spec
    return Config(name, plugins)


def _toml_value(v: str) -> str:
    return json.dumps(v)     # a JSON string is a valid TOML basic string


def save(root: Path, cfg: Config) -> None:
    lines = [
        "# The Claude Code plugins this project uses, each pinned to a release.",
        "# Managed with plugin-hub (add, remove, upgrade); after editing by hand, run",
        "# `plugin-hub generate`. Teammates run `plugin-hub sync` after cloning.",
        "",
        "[project]",
        f"name = {_toml_value(cfg.name)}",
        "",
        "[plugins]",
    ]
    for plugin in sorted(cfg.plugins):
        spec = cfg.plugins[plugin]
        if set(spec) == {"version"}:
            lines.append(f"{plugin} = {_toml_value(spec['version'])}")
        else:
            inner = ", ".join(f"{k} = {_toml_value(spec[k])}" for k in sorted(spec))
            lines.append(f"{plugin} = {{ {inner} }}")
    (root / CONFIG).write_text("\n".join(lines) + "\n")


def pinned_source(plugin: str, spec: dict, catalogue: dict[str, dict]) -> dict:
    """The marketplace `source` for one pinned plugin."""
    if any(k in spec for k in SOURCE_KEYS):
        if "repo" in spec:
            source = {"source": "github", "repo": spec["repo"]}
            if "path" in spec:
                source = {"source": "git-subdir", "url": f"https://github.com/{spec['repo']}.git",
                          "path": spec["path"]}
        elif "path" in spec:
            source = {"source": "git-subdir", "url": spec["url"], "path": spec["path"]}
        else:
            source = {"source": "url", "url": spec["url"]}
    elif plugin in catalogue:
        source = dict(catalogue[plugin]["source"])
    else:
        raise ProjectError(f"{plugin} is not in the catalogue; give its repo or url in "
                           f"{CONFIG}, or check the name with `plugin-hub list`")
    if source.get("source") not in ("github", "url", "git-subdir"):
        raise ProjectError(f"{plugin}: a {source.get('source')!r} source cannot be pinned "
                           f"to a git ref")
    for k in ("ref", "sha"):
        source.pop(k, None)
    if "version" in spec:
        source["ref"] = tag_for(plugin, spec["version"])
    elif "ref" in spec:
        source["ref"] = spec["ref"]
    else:
        source["sha"] = spec["sha"]
    return source


def _read(path: Path) -> dict | None:
    return json.loads(path.read_text()) if path.exists() else None


def bundle_version(previous: dict | None, deps: list[str]) -> str:
    """The bundle's version: bumped whenever its dependency list changes.

    Claude Code refreshes an installed plugin only when its version string
    changes, so an unbumped bundle would never pick up an added plugin.
    """
    if previous is None:
        return "1.0.0"
    if previous.get("dependencies") == deps:
        return previous["version"]
    major, minor, patch = (int(p) for p in previous["version"].split("."))
    return f"{major}.{minor}.{patch + 1}"


def generate(root: Path, cfg: Config, catalogue: dict[str, dict]) -> list[Path]:
    """Write the marketplace, the bundle and the settings. Returns what changed."""
    hub = root / HUB_DIR
    bundle_manifest = hub / cfg.bundle / ".claude-plugin" / "plugin.json"
    deps = sorted(cfg.plugins)
    entries = []
    for plugin in deps:
        entry = {"name": plugin, "source": pinned_source(plugin, cfg.plugins[plugin], catalogue)}
        if plugin in catalogue and "description" in catalogue[plugin]:
            entry["description"] = catalogue[plugin]["description"]
        entries.append(entry)
    entries.append({
        "name": cfg.bundle, "source": f"./{cfg.bundle}",
        "description": f"Installs every plugin {cfg.name} uses, at the release pinned in "
                       f"{CONFIG}. Carries nothing of its own.",
    })
    marketplace = {
        "name": cfg.marketplace,
        "owner": {"name": "plugin-hub"},
        "metadata": {"description": f"The plugins {cfg.name} uses, each pinned to a release. "
                                    f"Generated from {CONFIG} by plugin-hub; do not edit."},
        "plugins": entries,
    }
    bundle = {
        "name": cfg.bundle,
        "version": bundle_version(_read(bundle_manifest), deps),
        "description": f"Every plugin {cfg.name} uses, as pinned in {CONFIG}.",
        "dependencies": deps,
    }

    settings_path = root / SETTINGS
    settings = _read(settings_path) or {}
    known = settings.setdefault("extraKnownMarketplaces", {})
    # Relative, so the committed file works in every clone. `claude plugin
    # marketplace add` would write an absolute path here; sync registers the
    # marketplace at local scope instead, which leaves this entry alone.
    known[cfg.marketplace] = {"source": {"source": "directory", "path": f"./{HUB_DIR.as_posix()}"}}
    enabled = settings.setdefault("enabledPlugins", {})
    for key in [k for k in enabled if k.endswith(f"@{cfg.marketplace}")]:
        del enabled[key]
    for plugin in [*deps, cfg.bundle]:
        enabled[f"{plugin}@{cfg.marketplace}"] = True

    changed = [p for p in [ignore_local_settings(root)] if p]
    for path, doc in ((hub / ".claude-plugin" / "marketplace.json", marketplace),
                      (bundle_manifest, bundle), (settings_path, settings)):
        text = json.dumps(doc, indent=2) + "\n"
        if not path.exists() or path.read_text() != text:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            changed.append(path)
    return changed


def ignore_local_settings(root: Path) -> Path | None:
    """Make the project's own .gitignore cover .claude/settings.local.json.

    sync registers the marketplace at local scope, which writes this machine's
    absolute path into that file. Only the repo's rules count: a rule in one
    person's global excludes file does not stop a teammate committing it.
    """
    if not (root / ".git").exists():
        return None
    out = subprocess.run(["git", "-c", "core.excludesFile=/dev/null", "check-ignore", "-q",
                          LOCAL_SETTINGS.as_posix()], cwd=root, capture_output=True)
    if out.returncode == 0:
        return None
    gitignore = root / ".gitignore"
    text = gitignore.read_text() if gitignore.exists() else ""
    gitignore.write_text(text + ("" if not text or text.endswith("\n") else "\n")
                         + f"{LOCAL_SETTINGS.as_posix()}\n")
    return gitignore


def stale(root: Path, cfg: Config) -> list[str]:
    """How the generated marketplace disagrees with plugins.toml, offline."""
    market = _read(root / HUB_DIR / ".claude-plugin" / "marketplace.json")
    if market is None:
        return [f"{HUB_DIR}/.claude-plugin/marketplace.json does not exist"]
    problems = []
    if market.get("name") != cfg.marketplace:
        problems.append(f"marketplace is named {market.get('name')!r}, expected {cfg.marketplace!r}")
    listed = {p["name"]: p["source"] for p in market.get("plugins", []) if p["name"] != cfg.bundle}
    for plugin, spec in cfg.plugins.items():
        source = listed.get(plugin)
        if source is None:
            problems.append(f"{plugin} is in {CONFIG} but not in the marketplace")
            continue
        want = (("ref", tag_for(plugin, spec["version"])) if "version" in spec
                else ("ref", spec["ref"]) if "ref" in spec else ("sha", spec["sha"]))
        if source.get(want[0]) != want[1]:
            problems.append(f"{plugin}: marketplace pins {source.get('ref') or source.get('sha')}, "
                            f"{CONFIG} pins {want[1]}")
    for plugin in listed.keys() - cfg.plugins.keys():
        problems.append(f"{plugin} is in the marketplace but not in {CONFIG}")
    return problems


def sync_plan(cfg: Config, installed: dict[str, str]) -> list[list[str]]:
    """`claude plugin` commands that make this project's installs match the pins.

    `installed` maps plugin name to version, for this project's installs from its
    own marketplace. Updating the bundle does not move its dependencies, so each
    pinned plugin already installed is updated on its own; an update to the
    version already installed is a no-op.
    """
    m, scope = cfg.marketplace, ["--scope", "project"]
    plan = []
    verb = "update" if cfg.bundle in installed else "install"
    plan.append(["plugin", verb, f"{cfg.bundle}@{m}", *scope])
    for plugin in sorted(cfg.plugins):
        if plugin in installed:
            plan.append(["plugin", "update", f"{plugin}@{m}", *scope])
        elif verb == "update":
            # Added since the bundle was installed: the bundle update does not
            # install new dependencies either.
            plan.append(["plugin", "install", f"{plugin}@{m}", *scope])
    for plugin in sorted(installed.keys() - cfg.plugins.keys() - {cfg.bundle}):
        plan.append(["plugin", "uninstall", f"{plugin}@{m}", *scope])
    return plan
