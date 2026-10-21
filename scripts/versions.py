#!/usr/bin/env python3
"""Plugin and package version bookkeeping, for CI and for people.

    versions.py check-bumps --base origin/main   changed plugin/package => version bumped
    versions.py untagged                         releases whose tag does not exist yet
    versions.py pins [--tags N] [--at SHA]       every package pin users can be running
    versions.py report [--check FILE]            the plugin -> package table (docs/pins.md)
    versions.py cli <dir> [--at REF]             the console script packages/<dir> installs

Standard library only, and it shells out to git rather than importing anything:
CI runs it before any package is installed.

Two tag families live in one repo, and both are cut from main by the release
workflow, never by hand from a branch:

    <plugin>--v<version>        what `claude plugin tag` writes, and what a
                                bundle's version range and a ref-pinned
                                marketplace resolve against
    pkg-<dir>--v<version>       what a skill's `uvx --from ...@<tag>` pins

A plugin tag freezes its SKILL.md, and the SKILL.md holds the package pin, so
"which package does plugin version X run" is answered by reading the skill at
the plugin's tag. Nothing else records the mapping, and nothing else needs to.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# A git URL, a ref, and the package subdirectory, plus an optional
# `name[extras] @ ` in front so that CI installs exactly what the skill does --
# a pin whose extra went missing would otherwise pass here and fail for users.
# The CLI's name comes from the package's [project.scripts]: see `cli`.
PIN = re.compile(r"(?:[a-z0-9-]+\[[a-z0-9,-]+\] @ )?"
                 r"git\+https://github\.com/[^\"'\s]+#subdirectory=packages/[a-z0-9-]+")
# The ref holds no "@" or space, so this skips the " @ " of a `name[extras] @` prefix.
PIN_PARTS = re.compile(r"@(?P<ref>[^#@\s]+)#subdirectory=packages/(?P<pkg>[a-z0-9-]+)$")

# Changes under these never alter what a user runs, so they need no bump.
# uv.lock included: `uvx --from git+...` resolves fresh and never reads it.
PACKAGE_EXEMPT = ("tests/", "README.md", "LICENSE", "uv.lock")


def git(*args: str, check: bool = True) -> str:
    out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    if check and out.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed:\n{out.stderr}")
    return out.stdout


def show(ref: str, path: str) -> str | None:
    """A file's contents at a ref, or None when it does not exist there."""
    out = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=ROOT,
                         capture_output=True, text=True)
    return out.stdout if out.returncode == 0 else None


def semver(v: str) -> tuple[int, ...]:
    core = v.split("-", 1)[0].split("+", 1)[0]
    try:
        return tuple(int(p) for p in core.split("."))
    except ValueError:
        sys.exit(f"not a version: {v!r}")


@dataclass(frozen=True)
class Release:
    kind: str       # "plugin" or "package"
    name: str       # plugin name, or package directory
    path: str       # repo-relative directory
    version_file: str

    @property
    def tag_prefix(self) -> str:
        return f"{self.name}--v" if self.kind == "plugin" else f"pkg-{self.name}--v"

    def version_at(self, ref: str | None) -> str | None:
        text = (ROOT / self.version_file).read_text() if ref is None else show(ref, self.version_file)
        if text is None:
            return None
        if self.kind == "plugin":
            return json.loads(text).get("version")
        return tomllib.loads(text)["project"]["version"]

    def tag(self, version: str) -> str:
        return f"{self.tag_prefix}{version}"


def releases() -> list[Release]:
    found = []
    for manifest in sorted(ROOT.glob("plugins/*/.claude-plugin/plugin.json")):
        d = manifest.parent.parent
        name = json.loads(manifest.read_text())["name"]
        found.append(Release("plugin", name, d.relative_to(ROOT).as_posix(),
                             manifest.relative_to(ROOT).as_posix()))
    for pyproject in sorted(ROOT.glob("packages/*/pyproject.toml")):
        d = pyproject.parent
        found.append(Release("package", d.name, d.relative_to(ROOT).as_posix(),
                             pyproject.relative_to(ROOT).as_posix()))
    return found


def tags_for(rel: Release) -> list[str]:
    """This release line's tags, newest version first."""
    tags = git("tag", "--list", f"{rel.tag_prefix}*").split()
    return sorted(tags, key=lambda t: semver(t[len(rel.tag_prefix):]), reverse=True)


# --- check-bumps -------------------------------------------------------------

def check_bumps(base: str) -> int:
    changed = git("diff", "--name-only", f"{base}...HEAD").split()
    problems = []

    marketplace = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
    listed = {p["name"]: p for p in marketplace["plugins"]}

    for rel in releases():
        if rel.kind == "package":
            # `<cli> --version` prints __version__, not pyproject's version.
            new = rel.version_at(None)
            for init in sorted((ROOT / rel.path).glob("src/*/__init__.py")):
                m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', init.read_text(), re.M)
                if m and m[1] != new:
                    problems.append(f"{rel.name}: {init.relative_to(ROOT)} says __version__ "
                                    f"= {m[1]!r}, pyproject.toml says {new!r}")

        touched = [f for f in changed if f.startswith(rel.path + "/")]
        if rel.kind == "package":
            touched = [f for f in touched
                       if not f[len(rel.path) + 1:].startswith(PACKAGE_EXEMPT)]

        if rel.kind == "plugin":
            entry = listed.get(rel.name)
            if entry is None:
                problems.append(f"{rel.name}: not listed in .claude-plugin/marketplace.json")
            elif "version" in entry and entry["version"] != rel.version_at(None):
                # `claude plugin tag` refuses to tag when these disagree, and the
                # release job would then fail after merge rather than here.
                problems.append(f"{rel.name}: marketplace entry says {entry['version']}, "
                                f"plugin.json says {rel.version_at(None)}")

        if not touched:
            continue
        old, new = rel.version_at(base), rel.version_at(None)
        if new is None:
            problems.append(f"{rel.name}: {rel.version_file} has no version")
        elif old is None:
            print(f"ok    {rel.kind} {rel.name} {new} (new)")
        elif semver(new) <= semver(old):
            what = (f"the version is still {old}" if new == old
                    else f"the version went backwards, {old} -> {new}")
            problems.append(
                f"{rel.name}: {len(touched)} file(s) changed under {rel.path}/ but "
                f"{what}. Bump {rel.version_file} above {old}."
                + (" Installed copies only refresh when this string changes, so without "
                   "a bump nobody who already has the plugin receives this change."
                   if rel.kind == "plugin" else ""))
        else:
            print(f"ok    {rel.kind} {rel.name} {old} -> {new}")

        # Only for an actual bump: an unchanged version is already reported above.
        if new and new != old and rel.tag(new) in git("tag", "--list", rel.tag(new)).split():
            problems.append(f"{rel.name}: tag {rel.tag(new)} already exists; "
                            f"a released version cannot change")

    for p in problems:
        print(f"FAIL  {p}")
    return 1 if problems else 0


# --- untagged ----------------------------------------------------------------

def pending() -> list[tuple[Release, str]]:
    """Releases whose current version has no tag yet, packages first."""
    out = []
    for rel in sorted(releases(), key=lambda r: r.kind != "package"):
        v = rel.version_at(None)
        if v and not git("tag", "--list", rel.tag(v)).strip():
            out.append((rel, v))
    return out


def untagged() -> int:
    """`kind name version tag path`, one line per release still to be cut.

    Packages print first: a plugin released in the same push may pin them.
    """
    # Nothing at all when there is nothing to cut: an empty line would be read
    # by the release loops as a release with no name.
    for rel, v in pending():
        print(f"{rel.kind}\t{rel.name}\t{v}\t{rel.tag(v)}\t{rel.path}")
    return 0


# --- pins --------------------------------------------------------------------

def pins_at(ref: str | None, plugin: Release) -> set[str]:
    if ref is None:
        files = [p for p in (ROOT / plugin.path).rglob("*") if p.is_file()]
        texts = [p.read_text(errors="replace") for p in files]
    else:
        names = git("ls-tree", "-r", "--name-only", ref, "--", plugin.path).split()
        texts = [show(ref, n) or "" for n in names]
    return {m for t in texts for m in PIN.findall(t)}


def collect_pins(n_tags: int) -> dict[str, set[str]]:
    """pin spec -> where it is referenced ("main", or plugin tags)."""
    seen: dict[str, set[str]] = {}
    for rel in releases():
        if rel.kind != "plugin":
            continue
        for pin in pins_at(None, rel):
            seen.setdefault(pin, set()).add(f"{rel.name}@HEAD")
        for tag in tags_for(rel)[:n_tags]:
            for pin in pins_at(tag, rel):
                seen.setdefault(pin, set()).add(tag)
    return seen


def install_spec(pin: str, at: str | None, to_cut: set[str]) -> str:
    """What to hand `uvx --from` to check that `pin` will work for a user.

    Normally the pin itself. The exception is a pin to a package tag that does
    not exist yet but that the release workflow will cut from this very commit
    -- the package's version here has no tag. That tag cannot be fetched until
    after merge, and the release workflow only runs once this check has passed,
    so insisting on the tag would deadlock the first release of every package.
    The commit holds the same files the tag will, so it is checked instead.

    A pin to any other missing tag -- a typo, a version nobody bumped to --
    is left alone, and fails as it should.
    """
    m = PIN_PARTS.search(pin)
    if at and m and m["ref"] in to_cut:
        return pin.replace(f"@{m['ref']}#", f"@{at}#")
    return pin


def pins(n_tags: int, at: str | None) -> int:
    """`pin  where  spec-to-install`, one line per distinct pin."""
    to_cut = {rel.tag(v) for rel, v in pending() if rel.kind == "package"}
    for pin, where in sorted(collect_pins(n_tags).items()):
        print(f"{pin}\t{' '.join(sorted(where))}\t{install_spec(pin, at, to_cut)}")
    return 0


# --- report ------------------------------------------------------------------

def report_text(n_tags: int) -> str:
    lines = [
        "# Plugin → package pins",
        "",
        "Generated by `scripts/versions.py report`; CI fails when it is stale.",
        "This file shows main only: tags are cut after merge, so a checked-in",
        "table that listed them would be stale the moment a release landed.",
        "The nightly workflow's summary shows the same table with the supported",
        "tags added (`report --tags 3`). A plugin tag freezes its skills, so each",
        "of those rows is what a project pinned to that tag actually runs.",
        "",
        "| Plugin | Plugin version | Package | Package tag |",
        "|---|---|---|---|",
    ]
    for rel in releases():
        if rel.kind != "plugin":
            continue
        refs = [(None, f"{rel.version_at(None)} (main)")]
        refs += [(t, t[len(rel.tag_prefix):]) for t in tags_for(rel)[:n_tags]]
        for ref, label in refs:
            found = sorted(pins_at(ref, rel))
            if not found:
                lines.append(f"| `{rel.name}` | {label} | — | — |")
            for pin in found:
                m = PIN_PARTS.search(pin)
                lines.append(f"| `{rel.name}` | {label} | `{m['pkg']}` | `{m['ref']}` |")
    return "\n".join(lines) + "\n"


def report(n_tags: int, check: str | None) -> int:
    text = report_text(n_tags)
    if check is None:
        print(text, end="")
        return 0
    current = (ROOT / check).read_text() if (ROOT / check).exists() else ""
    if current != text:
        print(f"{check} is stale. Regenerate it:\n"
              f"  python scripts/versions.py report > {check}")
        return 1
    print(f"{check} is current")
    return 0


# --- cli ---------------------------------------------------------------------

def cli(pkg_dir: str, at: str | None) -> int:
    """The console script a package installs, which is what `uvx` must run.

    The script named after the directory when there is one -- packages/onto-query
    installs both `onto-query` and its short alias `oq` -- otherwise the only
    script the package declares. Read at `at` -- the ref a pin names -- since an
    old plugin tag can pin a package that has since been renamed or moved out of
    the repo. A ref that does not exist yet (a tag this commit's release will
    cut) falls back to the working tree.
    """
    path = f"packages/{pkg_dir}/pyproject.toml"
    text = show(at, path) if at else None
    if text is None and (ROOT / path).exists():
        text = (ROOT / path).read_text()
    if text is None:
        sys.exit(f"no package at packages/{pkg_dir}" + (f" at {at} or HEAD" if at else ""))
    scripts = tomllib.loads(text)["project"].get("scripts", {})
    if pkg_dir in scripts:
        print(pkg_dir)
        return 0
    if len(scripts) != 1:
        sys.exit(f"packages/{pkg_dir} must declare a console script named {pkg_dir!r}, "
                 f"or exactly one console script; found {sorted(scripts) or 'none'}")
    print(next(iter(scripts)))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("check-bumps")
    b.add_argument("--base", required=True, help="ref to compare against, e.g. origin/main")
    sub.add_parser("untagged")
    p = sub.add_parser("pins")
    # The supported window: the newest N releases of each plugin. Older tags
    # still install, but nobody is told they work.
    p.add_argument("--tags", type=int, default=3)
    p.add_argument("--at", metavar="SHA",
                   help="commit to install from for a package tag this commit's release will cut")
    r = sub.add_parser("report")
    r.add_argument("--tags", type=int, default=0, help="also show the newest N tags per plugin")
    r.add_argument("--check", metavar="FILE")
    c = sub.add_parser("cli")
    c.add_argument("dir", help="package directory under packages/")
    c.add_argument("--at", metavar="REF", help="read the package as it is at REF")
    a = ap.parse_args()

    if a.cmd == "check-bumps":
        return check_bumps(a.base)
    if a.cmd == "untagged":
        return untagged()
    if a.cmd == "pins":
        return pins(a.tags, a.at)
    if a.cmd == "cli":
        return cli(a.dir, a.at)
    return report(a.tags, a.check)


if __name__ == "__main__":
    sys.exit(main())
