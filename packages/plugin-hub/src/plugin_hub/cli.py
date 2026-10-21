"""plugin-hub: pin a project's Claude Code plugins in plugins.toml, install exactly that.

    plugin-hub list                     every plugin in the catalogue, with releases
    plugin-hub add NAME[@VERSION]...    pin plugins (newest release by default)
    plugin-hub remove NAME...           drop plugins
    plugin-hub upgrade [NAME...]        move pins to the newest release
    plugin-hub outdated                 pins with a newer release
    plugin-hub generate                 rewrite the generated files from plugins.toml
    plugin-hub sync                     install exactly what plugins.toml pins
    plugin-hub catalogue                build the catalogue from sources.toml

Run from the project's root. add, remove and upgrade regenerate the files Claude
Code reads; sync is what a teammate runs after cloning, and after every pull
that changes plugins.toml.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import __version__, catalogue, project, remote


def _catalogue() -> dict[str, dict]:
    return catalogue.load(os.environ.get("PLUGIN_HUB_CATALOGUE", catalogue.DEFAULT_LOCATION),
                          remote.read_json)


def _releases(entries: list[dict]) -> dict[str, list[str] | None]:
    """Released versions per plugin, None where the source has no git tags."""
    def one(entry: dict) -> tuple[str, list[str] | None]:
        url = remote.git_url(entry["source"])
        return entry["name"], (remote.versions(url, entry["name"]) if url else None)
    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(pool.map(one, entries))


def cmd_list(args: argparse.Namespace) -> int:
    cat = _catalogue()
    cfg = project.load(Path.cwd())
    releases = _releases(list(cat.values()))
    width = max(map(len, cat), default=0)
    for name in sorted(cat):
        rel = releases[name]
        newest = rel[0] if rel else ("no releases" if rel is not None else "not git")
        mark = "*" if name in cfg.plugins else " "
        print(f"{mark} {name:<{width}}  {newest:<12}  {cat[name].get('homepage', '')}")
        if args.long and cat[name].get("description"):
            print(f"  {'':<{width}}  {cat[name]['description']}")
    if cfg.plugins:
        print(f"\n* pinned in {project.CONFIG}")
    return 0


def _resolve(name: str, version: str | None, cat: dict[str, dict]) -> str:
    entry = cat.get(name)
    if entry is None:
        raise project.ProjectError(f"{name} is not in the catalogue; see `plugin-hub list`")
    url = remote.git_url(entry["source"])
    found = remote.versions(url, name) if url else []
    if not found:
        raise project.ProjectError(
            f"{name} has no releases (no {name}--v* tags in its repo). Pin it to a commit "
            f"in {project.CONFIG} instead: {name} = {{ sha = \"<40-char commit>\" }}")
    version = version or found[0]
    if version not in found:
        raise project.ProjectError(f"{name} {version} is not released; releases: "
                                   + ", ".join(found[:10]))
    deps = (remote.manifest(entry["source"], remote.tag_for(name, version)) or {}).get(
        "dependencies")
    if deps:
        # Its dependencies name another marketplace, which Claude Code would
        # install from at whatever that marketplace holds -- unpinned, and shared
        # with every other project on the machine.
        names = [d if isinstance(d, str) else d["name"] for d in deps]
        names = [n.split("@", 1)[0] for n in names]
        raise project.ProjectError(
            f"{name} {version} is a bundle of {', '.join(names)}; its dependencies "
            f"would install outside this project's pins. Add those instead:\n"
            f"  plugin-hub add {' '.join(names)}")
    return version


def _write(cfg: project.Config, cat: dict[str, dict]) -> None:
    root = Path.cwd()
    project.save(root, cfg)
    for path in project.generate(root, cfg, cat):
        print(f"wrote {path.relative_to(root)}")
    print(f"Next: `plugin-hub sync` to install, then commit {project.CONFIG} and .claude/.")


def cmd_add(args: argparse.Namespace) -> int:
    cat = _catalogue()
    cfg = project.load(Path.cwd())
    for item in args.plugins:
        name, _, version = item.partition("@")
        version = _resolve(name, version or None, cat)
        old = cfg.plugins.get(name, {}).get("version")
        cfg.plugins[name] = {"version": version}
        print(f"{name} {old + ' -> ' if old and old != version else ''}{version}")
    _write(cfg, cat)
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    cfg = project.load(Path.cwd())
    missing = [n for n in args.plugins if n not in cfg.plugins]
    if missing:
        raise project.ProjectError(f"not in {project.CONFIG}: {', '.join(missing)}")
    for name in args.plugins:
        del cfg.plugins[name]
        print(f"removed {name}")
    _write(cfg, _catalogue())
    print("`plugin-hub sync` also uninstalls them from this project.")
    return 0


def _behind(cfg: project.Config, cat: dict[str, dict],
            names: list[str] | None) -> list[tuple[str, str, str]]:
    pinned = {n: s for n, s in cfg.plugins.items() if "version" in s and (not names or n in names)}
    entries = [{"name": n, "source": project.pinned_source(n, s, cat)} for n, s in pinned.items()]
    releases = _releases(entries)
    return [(n, pinned[n]["version"], releases[n][0]) for n in sorted(pinned)
            if releases[n] and remote.semver(releases[n][0]) > remote.semver(pinned[n]["version"])]


def cmd_outdated(args: argparse.Namespace) -> int:
    cfg = project.load(Path.cwd())
    behind = _behind(cfg, _catalogue(), None)
    for name, have, newest in behind:
        print(f"{name}  {have} -> {newest}")
    unversioned = [n for n, s in cfg.plugins.items() if "version" not in s]
    if unversioned:
        print(f"pinned to a ref or commit, not checked: {', '.join(sorted(unversioned))}")
    if not behind:
        print("every versioned pin is the newest release")
    return 0


def cmd_upgrade(args: argparse.Namespace) -> int:
    cfg = project.load(Path.cwd())
    unknown = [n for n in args.plugins if n not in cfg.plugins]
    if unknown:
        raise project.ProjectError(f"not in {project.CONFIG}: {', '.join(unknown)}")
    cat = _catalogue()
    behind = _behind(cfg, cat, args.plugins or None)
    for name, have, newest in behind:
        cfg.plugins[name] = {"version": newest}
        print(f"{name} {have} -> {newest}")
    if not behind:
        print("already at the newest releases")
        return 0
    _write(cfg, cat)
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    root = Path.cwd()
    cfg = project.load(root)
    changed = project.generate(root, cfg, _catalogue())
    for path in changed:
        print(f"wrote {path.relative_to(root)}")
    if not changed:
        print("generated files are current")
    return 0


def _claude(*args: str) -> subprocess.CompletedProcess:
    out = subprocess.run(["claude", *args], capture_output=True, text=True)
    if out.returncode != 0:
        raise project.ProjectError(f"claude {' '.join(args)} failed:\n{out.stdout}{out.stderr}")
    return out


def _installed(cfg: project.Config, root: Path) -> dict[str, str]:
    rows = json.loads(_claude("plugin", "list", "--json").stdout or "[]")
    here = str(root.resolve())
    suffix = f"@{cfg.marketplace}"
    return {r["id"][: -len(suffix)]: r.get("version", "")
            for r in rows
            if r.get("id", "").endswith(suffix) and r.get("scope") == "project"
            and str(Path(r.get("projectPath", "")).resolve()) == here}


def cmd_sync(args: argparse.Namespace) -> int:
    root = Path.cwd()
    if shutil.which("claude") is None:
        raise project.ProjectError("`claude` is not on PATH; install Claude Code first")
    cfg = project.load(root)
    problems = project.stale(root, cfg)
    if problems:
        raise project.ProjectError("the generated files do not match plugins.toml:\n  "
                                   + "\n  ".join(problems) + "\nRun `plugin-hub generate`.")
    # Local scope: registering writes the marketplace's absolute path, which
    # belongs in the uncommitted settings file, not the shared one.
    _claude("plugin", "marketplace", "add", str((root / project.HUB_DIR).resolve()),
            "--scope", "local")
    _claude("plugin", "marketplace", "update", cfg.marketplace)
    for cmd in project.sync_plan(cfg, _installed(cfg, root)):
        print("claude " + " ".join(cmd))
        print("  " + (_claude(*cmd).stdout.strip().splitlines() or [""])[-1])
    after = _installed(cfg, root)
    print()
    status = 0
    for name in sorted(cfg.plugins):
        want = cfg.plugins[name].get("version")
        have = after.get(name)
        ok = have is not None and (want is None or have == want)
        status |= not ok
        print(f"{'ok ' if ok else 'BAD'} {name} {have or 'not installed'}"
              + (f" (pinned {want})" if want and have != want else ""))
    print("\nRestart Claude Code in this project to load the changes.")
    return status


def cmd_catalogue(args: argparse.Namespace) -> int:
    sources = catalogue.read_sources(Path(args.sources))
    text = json.dumps(catalogue.build(sources, remote.read_json), indent=2) + "\n"
    out = Path(args.out)
    if args.check:
        if not out.exists() or out.read_text() != text:
            print(f"{out} is stale; run `plugin-hub catalogue`")
            return 1
        print(f"{out} is current")
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    print(f"wrote {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="plugin-hub", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"plugin-hub {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list", help="every plugin in the catalogue, with releases")
    p.add_argument("-l", "--long", action="store_true", help="also show descriptions")
    p.set_defaults(fn=cmd_list)
    p = sub.add_parser("add", help="pin plugins, newest release unless NAME@VERSION")
    p.add_argument("plugins", nargs="+", metavar="NAME[@VERSION]")
    p.set_defaults(fn=cmd_add)
    p = sub.add_parser("remove", help="drop plugins from plugins.toml")
    p.add_argument("plugins", nargs="+", metavar="NAME")
    p.set_defaults(fn=cmd_remove)
    p = sub.add_parser("upgrade", help="move pins to the newest release")
    p.add_argument("plugins", nargs="*", metavar="NAME")
    p.set_defaults(fn=cmd_upgrade)
    sub.add_parser("outdated", help="pins with a newer release").set_defaults(fn=cmd_outdated)
    sub.add_parser("generate", help="rewrite the generated files").set_defaults(fn=cmd_generate)
    sub.add_parser("sync", help="install exactly what plugins.toml pins").set_defaults(fn=cmd_sync)
    p = sub.add_parser("catalogue", help="build the catalogue from sources.toml")
    p.add_argument("--sources", default="sources.toml")
    p.add_argument("--out", default=".claude-plugin/marketplace.json")
    p.add_argument("--check", action="store_true", help="fail if the file is stale")
    p.set_defaults(fn=cmd_catalogue)
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except (project.ProjectError, catalogue.CatalogueError, remote.RemoteError) as e:
        print(f"plugin-hub: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
