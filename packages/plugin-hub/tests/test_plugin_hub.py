"""Offline tests: catalogue building, plugins.toml, generated files, sync plans.

The real `claude` and the real GitHub are exercised by hand against throwaway
Claude configs (see the repo README); nothing here touches the network.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from plugin_hub import catalogue, cli, project, remote

CAP = {"name": "cap_skills", "plugins": [
    {"name": "cap-tools", "source": "./plugins/cap-tools", "description": "CAP tools"}]}
ATLAS = {"name": "atlas", "metadata": {"pluginRoot": "./plugins"}, "plugins": [
    {"name": "remote-h5ad-obs", "source": "remote-h5ad-obs", "description": "obs"},
    {"name": "elsewhere", "source": {"source": "github", "repo": "o/elsewhere", "ref": "v9"}}]}
SOURCES = [{"repo": "org/cap"}, {"repo": "org/atlas"}]


def fake_fetch(docs: dict[str, dict]):
    def fetch(url: str) -> dict:
        for repo, doc in docs.items():
            if f"/{repo}/" in url:
                return doc
        raise remote.RemoteError(f"no {url}")
    return fetch


@pytest.fixture
def cat() -> dict[str, dict]:
    built = catalogue.build(SOURCES, fake_fetch({"org/cap": CAP, "org/atlas": ATLAS}))
    return {p["name"]: p for p in built["plugins"]}


# --- catalogue ---------------------------------------------------------------

def test_relative_sources_point_back_into_their_repo(cat: dict) -> None:
    assert cat["cap-tools"]["source"] == {
        "source": "git-subdir", "url": "https://github.com/org/cap.git", "path": "plugins/cap-tools"}


def test_plugin_root_applies_to_bare_relative_sources(cat: dict) -> None:
    assert cat["remote-h5ad-obs"]["source"]["path"] == "plugins/remote-h5ad-obs"


def test_external_sources_are_kept(cat: dict) -> None:
    assert cat["elsewhere"]["source"]["repo"] == "o/elsewhere"


def test_catalogue_entries_carry_description_and_homepage(cat: dict) -> None:
    assert cat["cap-tools"]["description"] == "CAP tools"
    assert cat["cap-tools"]["homepage"] == "https://github.com/org/cap"


def test_catalogue_is_sorted_and_named() -> None:
    built = catalogue.build(SOURCES, fake_fetch({"org/cap": CAP, "org/atlas": ATLAS}))
    assert built["name"] == "plugin-hub"
    names = [p["name"] for p in built["plugins"]]
    assert names == sorted(names)


def test_duplicate_plugin_names_across_repos_fail() -> None:
    with pytest.raises(catalogue.CatalogueError, match="both org/cap and org/atlas"):
        catalogue.build(SOURCES, fake_fetch({"org/cap": CAP, "org/atlas": CAP}))


def test_unreachable_source_fails() -> None:
    with pytest.raises(catalogue.CatalogueError, match="no https://"):
        catalogue.build(SOURCES, fake_fetch({"org/cap": CAP}))


def test_source_ref_is_used(tmp_path: Path) -> None:
    assert "/org/cap/v2/" in catalogue.marketplace_url({"repo": "org/cap", "ref": "v2"})
    assert "/org/cap/HEAD/" in catalogue.marketplace_url({"repo": "org/cap"})


# --- remote.versions, against a real git repo ----------------------------------

def test_versions_newest_first_by_number_not_text(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    for cmd in (["init", "-q"], ["-c", "user.email=t@t", "-c", "user.name=t",
                                 "commit", "-q", "--allow-empty", "-m", "x"]):
        subprocess.run(["git", *cmd], cwd=repo, check=True)
    for t in ("cap-tools--v0.9.0", "cap-tools--v0.10.0", "cap-tools--v1.0.0-rc1",
              "other--v5.0.0", "pkg-cap-tools--v3.0.0"):
        subprocess.run(["git", "tag", t], cwd=repo, check=True)
    assert remote.versions(f"file://{repo}", "cap-tools") == ["0.10.0", "0.9.0"]


# --- plugins.toml ------------------------------------------------------------

def test_missing_config_names_project_after_folder(tmp_path: Path) -> None:
    root = tmp_path / "My Project_2"
    root.mkdir()
    cfg = project.load(root)
    assert cfg.name == "my-project-2" and cfg.marketplace == "my-project-2-plugins"


def test_config_round_trips(tmp_path: Path) -> None:
    cfg = project.Config("p", {"cap-tools": {"version": "0.4.1"},
                               "x": {"repo": "o/x", "sha": "a" * 40}})
    project.save(tmp_path, cfg)
    assert project.load(tmp_path) == cfg


def test_config_needs_exactly_one_pin(tmp_path: Path) -> None:
    (tmp_path / "plugins.toml").write_text('[plugins]\nx = { repo = "o/x" }\n')
    with pytest.raises(project.ProjectError, match="exactly one of"):
        project.load(tmp_path)


# --- pinned sources ------------------------------------------------------------

def test_version_pins_the_release_tag(cat: dict) -> None:
    src = project.pinned_source("cap-tools", {"version": "0.4.0"}, cat)
    assert src["ref"] == "cap-tools--v0.4.0" and src["path"] == "plugins/cap-tools"


def test_sha_pin_replaces_any_catalogue_ref(cat: dict) -> None:
    src = project.pinned_source("elsewhere", {"sha": "b" * 40}, cat)
    assert src == {"source": "github", "repo": "o/elsewhere", "sha": "b" * 40}


def test_explicit_repo_and_path_need_no_catalogue() -> None:
    src = project.pinned_source("y", {"repo": "o/y", "path": "plugins/y", "ref": "main"}, {})
    assert src == {"source": "git-subdir", "url": "https://github.com/o/y.git",
                   "path": "plugins/y", "ref": "main"}


def test_unknown_plugin_fails() -> None:
    with pytest.raises(project.ProjectError, match="not in the catalogue"):
        project.pinned_source("nope", {"version": "1.0.0"}, {})


# --- generate ----------------------------------------------------------------

def read(root: Path, rel: str) -> dict:
    return json.loads((root / rel).read_text())


MARKET = ".claude/plugin-hub/.claude-plugin/marketplace.json"
BUNDLE = ".claude/plugin-hub/p-set/.claude-plugin/plugin.json"


def test_generate_writes_marketplace_bundle_and_settings(tmp_path: Path, cat: dict) -> None:
    cfg = project.Config("p", {"cap-tools": {"version": "0.4.0"},
                               "remote-h5ad-obs": {"version": "0.3.0"}})
    project.generate(tmp_path, cfg, cat)
    market = read(tmp_path, MARKET)
    assert market["name"] == "p-plugins"
    assert [e["name"] for e in market["plugins"]] == ["cap-tools", "remote-h5ad-obs", "p-set"]
    assert market["plugins"][-1]["source"] == "./p-set"
    bundle = read(tmp_path, BUNDLE)
    assert bundle["dependencies"] == ["cap-tools", "remote-h5ad-obs"]
    assert bundle["version"] == "1.0.0"
    settings = read(tmp_path, ".claude/settings.json")
    assert settings["extraKnownMarketplaces"]["p-plugins"]["source"] == {
        "source": "directory", "path": "./.claude/plugin-hub"}
    assert set(settings["enabledPlugins"]) == {
        "cap-tools@p-plugins", "remote-h5ad-obs@p-plugins", "p-set@p-plugins"}


def test_generate_keeps_unrelated_settings(tmp_path: Path, cat: dict) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude/settings.json").write_text(json.dumps({
        "permissions": {"allow": ["Bash(ls)"]},
        "enabledPlugins": {"other@elsewhere": True, "gone@p-plugins": True}}))
    project.generate(tmp_path, project.Config("p", {"cap-tools": {"version": "0.4.0"}}), cat)
    settings = read(tmp_path, ".claude/settings.json")
    assert settings["permissions"] == {"allow": ["Bash(ls)"]}
    assert settings["enabledPlugins"]["other@elsewhere"] is True
    assert "gone@p-plugins" not in settings["enabledPlugins"]


def test_bundle_version_bumps_only_when_its_plugins_change(tmp_path: Path, cat: dict) -> None:
    cfg = project.Config("p", {"cap-tools": {"version": "0.4.0"}})
    project.generate(tmp_path, cfg, cat)
    cfg.plugins["cap-tools"] = {"version": "0.4.1"}     # a re-pin: same plugins
    project.generate(tmp_path, cfg, cat)
    assert read(tmp_path, BUNDLE)["version"] == "1.0.0"
    cfg.plugins["remote-h5ad-obs"] = {"version": "0.3.0"}
    project.generate(tmp_path, cfg, cat)
    assert read(tmp_path, BUNDLE)["version"] == "1.0.1"


def test_generate_twice_changes_nothing(tmp_path: Path, cat: dict) -> None:
    cfg = project.Config("p", {"cap-tools": {"version": "0.4.0"}})
    project.generate(tmp_path, cfg, cat)
    assert project.generate(tmp_path, cfg, cat) == []


def test_generate_gitignores_local_settings_once(tmp_path: Path, cat: dict) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text(".venv/")      # no trailing newline
    cfg = project.Config("p", {"cap-tools": {"version": "0.4.0"}})
    project.generate(tmp_path, cfg, cat)
    project.generate(tmp_path, cfg, cat)
    assert (tmp_path / ".gitignore").read_text() == ".venv/\n.claude/settings.local.json\n"


def test_generate_leaves_gitignore_alone_outside_git(tmp_path: Path, cat: dict) -> None:
    project.generate(tmp_path, project.Config("p", {}), cat)
    assert not (tmp_path / ".gitignore").exists()


# --- stale ---------------------------------------------------------------------

def test_stale_is_empty_after_generate(tmp_path: Path, cat: dict) -> None:
    cfg = project.Config("p", {"cap-tools": {"version": "0.4.0"}})
    project.generate(tmp_path, cfg, cat)
    assert project.stale(tmp_path, cfg) == []


def test_stale_reports_hand_edited_pins(tmp_path: Path, cat: dict) -> None:
    cfg = project.Config("p", {"cap-tools": {"version": "0.4.0"}})
    project.generate(tmp_path, cfg, cat)
    cfg.plugins["cap-tools"] = {"version": "0.4.1"}
    cfg.plugins["remote-h5ad-obs"] = {"version": "0.3.0"}
    problems = project.stale(tmp_path, cfg)
    assert any("pins cap-tools--v0.4.0" in p for p in problems)
    assert any("remote-h5ad-obs is in plugins.toml" in p for p in problems)


def test_stale_without_generated_files(tmp_path: Path) -> None:
    assert project.stale(tmp_path, project.Config("p", {}))


# --- sync plan -------------------------------------------------------------------

def plan(cfg: project.Config, installed: dict[str, str]) -> list[str]:
    return [" ".join(c[1:3]) for c in project.sync_plan(cfg, installed)]


def test_fresh_clone_installs_the_bundle_only() -> None:
    cfg = project.Config("p", {"a": {"version": "1.0.0"}, "b": {"version": "1.0.0"}})
    assert plan(cfg, {}) == ["install p-set@p-plugins"]


def test_installed_project_updates_each_plugin() -> None:
    cfg = project.Config("p", {"a": {"version": "1.1.0"}})
    assert plan(cfg, {"p-set": "1.0.0", "a": "1.0.0"}) == [
        "update p-set@p-plugins", "update a@p-plugins"]


def test_added_plugin_is_installed_and_removed_one_uninstalled() -> None:
    cfg = project.Config("p", {"a": {"version": "1.0.0"}, "new": {"version": "1.0.0"}})
    assert plan(cfg, {"p-set": "1.0.0", "a": "1.0.0", "old": "2.0.0"}) == [
        "update p-set@p-plugins", "update a@p-plugins", "install new@p-plugins",
        "uninstall old@p-plugins"]


# --- cli ---------------------------------------------------------------------

def test_add_refuses_a_bundle(monkeypatch: pytest.MonkeyPatch, cat: dict) -> None:
    monkeypatch.setattr(remote, "versions", lambda url, name: ["0.4.0"])
    monkeypatch.setattr(remote, "manifest", lambda source, ref: {
        "name": "cap-tools", "dependencies": ["a@m", {"name": "b", "marketplace": "m"}]})
    with pytest.raises(project.ProjectError, match="plugin-hub add a b"):
        cli._resolve("cap-tools", None, cat)


def test_add_picks_newest_release(monkeypatch: pytest.MonkeyPatch, cat: dict) -> None:
    monkeypatch.setattr(remote, "versions", lambda url, name: ["0.4.1", "0.4.0"])
    monkeypatch.setattr(remote, "manifest", lambda source, ref: {"name": "cap-tools"})
    assert cli._resolve("cap-tools", None, cat) == "0.4.1"
    assert cli._resolve("cap-tools", "0.4.0", cat) == "0.4.0"
    with pytest.raises(project.ProjectError, match="not released"):
        cli._resolve("cap-tools", "9.9.9", cat)


def test_add_without_releases_suggests_a_sha(monkeypatch: pytest.MonkeyPatch, cat: dict) -> None:
    monkeypatch.setattr(remote, "versions", lambda url, name: [])
    with pytest.raises(project.ProjectError, match="sha ="):
        cli._resolve("cap-tools", None, cat)
