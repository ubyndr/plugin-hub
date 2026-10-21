"""scripts/versions.py, run as CI runs it, inside throwaway git repos.

The release workflow trusts this script to decide what gets tagged, and the
pull-request check trusts it to refuse an unbumped change. Either going quietly
wrong would only show up after a release, so each rule has a case here.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "versions.py"
PIN = ("git+https://github.com/example/repo@pkg-tool--v{v}"
       "#subdirectory=packages/tool")


def sh(repo: Path, *args: str) -> str:
    return subprocess.run(args, cwd=repo, check=True, capture_output=True, text=True).stdout


def run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "scripts/versions.py", *args], cwd=repo,
                          capture_output=True, text=True)


def write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def set_plugin_version(repo: Path, v: str) -> None:
    write(repo, "plugins/thing/.claude-plugin/plugin.json",
          json.dumps({"name": "thing", "version": v}))


def set_package_version(repo: Path, v: str) -> None:
    write(repo, "packages/tool/pyproject.toml",
          f'[project]\nname = "tool"\nversion = "{v}"\n')


def set_pin(repo: Path, v: str) -> None:
    write(repo, "plugins/thing/skills/thing/SKILL.md",
          f'Run:\n\n    uvx --from "{PIN.format(v=v)}" tool\n')


def commit(repo: Path, msg: str) -> None:
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-qm", msg)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    (r / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, r / "scripts" / "versions.py")
    write(r, ".claude-plugin/marketplace.json", json.dumps(
        {"name": "m", "plugins": [{"name": "thing", "source": "./plugins/thing"}]}))
    set_plugin_version(r, "1.0.0")
    set_pin(r, "0.1.0")
    set_package_version(r, "0.1.0")
    write(r, "packages/tool/src/tool/__init__.py", "")
    sh(r, "git", "init", "-q", "-b", "main")
    sh(r, "git", "config", "user.email", "t@example.com")
    sh(r, "git", "config", "user.name", "t")
    commit(r, "init")
    sh(r, "git", "checkout", "-qb", "feature")
    return r


# --- check-bumps -------------------------------------------------------------

def test_skill_edit_without_bump_fails(repo: Path) -> None:
    write(repo, "plugins/thing/skills/thing/SKILL.md", "changed\n")
    commit(repo, "edit skill")
    out = run(repo, "check-bumps", "--base", "main")
    assert out.returncode == 1
    assert "thing" in out.stdout and "still 1.0.0" in out.stdout


def test_unbumped_released_version_reports_once(repo: Path) -> None:
    # Forgetting to bump is one fault, not also "re-releasing" 1.0.0.
    sh(repo, "git", "tag", "thing--v1.0.0", "main")
    write(repo, "plugins/thing/skills/thing/SKILL.md", "changed\n")
    commit(repo, "edit skill")
    out = run(repo, "check-bumps", "--base", "main")
    assert out.returncode == 1
    assert out.stdout.count("FAIL") == 1 and "already exists" not in out.stdout


def test_skill_edit_with_bump_passes(repo: Path) -> None:
    write(repo, "plugins/thing/skills/thing/SKILL.md", "changed\n")
    set_plugin_version(repo, "1.0.1")
    commit(repo, "edit skill, bump")
    out = run(repo, "check-bumps", "--base", "main")
    assert out.returncode == 0, out.stdout
    assert "1.0.0 -> 1.0.1" in out.stdout


def test_version_going_backwards_fails(repo: Path) -> None:
    write(repo, "plugins/thing/skills/thing/SKILL.md", "changed\n")
    set_plugin_version(repo, "0.9.0")
    commit(repo, "downgrade")
    out = run(repo, "check-bumps", "--base", "main")
    assert out.returncode == 1
    assert "went backwards, 1.0.0 -> 0.9.0" in out.stdout and "still" not in out.stdout


def test_package_code_without_bump_fails(repo: Path) -> None:
    write(repo, "packages/tool/src/tool/__init__.py", "x = 1\n")
    commit(repo, "code")
    out = run(repo, "check-bumps", "--base", "main")
    assert out.returncode == 1
    assert "packages/tool/pyproject.toml" in out.stdout


def test_dunder_version_disagreeing_fails(repo: Path) -> None:
    write(repo, "packages/tool/src/tool/__init__.py", '__version__ = "0.1.0"\n')
    set_package_version(repo, "0.2.0")
    commit(repo, "bump pyproject only")
    out = run(repo, "check-bumps", "--base", "main")
    assert out.returncode == 1
    assert "__version__ = '0.1.0', pyproject.toml says '0.2.0'" in out.stdout


def test_package_tests_and_readme_need_no_bump(repo: Path) -> None:
    write(repo, "packages/tool/tests/test_x.py", "def test(): pass\n")
    write(repo, "packages/tool/README.md", "docs\n")
    commit(repo, "tests and docs")
    out = run(repo, "check-bumps", "--base", "main")
    assert out.returncode == 0, out.stdout


def test_package_lockfile_needs_no_bump(repo: Path) -> None:
    # uvx ignores uv.lock, so it never changes what a user runs.
    write(repo, "packages/tool/uv.lock", "version = 1\n")
    commit(repo, "lock")
    out = run(repo, "check-bumps", "--base", "main")
    assert out.returncode == 0, out.stdout


def test_new_plugin_passes(repo: Path) -> None:
    write(repo, "plugins/other/.claude-plugin/plugin.json",
          json.dumps({"name": "other", "version": "0.1.0"}))
    mp = json.loads((repo / ".claude-plugin/marketplace.json").read_text())
    mp["plugins"].append({"name": "other", "source": "./plugins/other"})
    write(repo, ".claude-plugin/marketplace.json", json.dumps(mp))
    commit(repo, "new plugin")
    out = run(repo, "check-bumps", "--base", "main")
    assert out.returncode == 0, out.stdout
    assert "(new)" in out.stdout


def test_plugin_missing_from_marketplace_fails(repo: Path) -> None:
    write(repo, "plugins/other/.claude-plugin/plugin.json",
          json.dumps({"name": "other", "version": "0.1.0"}))
    commit(repo, "unlisted plugin")
    out = run(repo, "check-bumps", "--base", "main")
    assert out.returncode == 1
    assert "not listed" in out.stdout


def test_marketplace_version_disagreeing_fails(repo: Path) -> None:
    # `claude plugin tag` refuses this; catch it before merge, not in the release job.
    write(repo, ".claude-plugin/marketplace.json", json.dumps(
        {"name": "m", "plugins": [{"name": "thing", "source": "./plugins/thing",
                                   "version": "2.0.0"}]}))
    commit(repo, "marketplace version")
    out = run(repo, "check-bumps", "--base", "main")
    assert out.returncode == 1
    assert "marketplace entry says 2.0.0" in out.stdout


def test_bump_to_an_already_tagged_version_fails(repo: Path) -> None:
    sh(repo, "git", "tag", "thing--v1.0.1", "main")
    write(repo, "plugins/thing/skills/thing/SKILL.md", "changed\n")
    set_plugin_version(repo, "1.0.1")
    commit(repo, "reuse a released version")
    out = run(repo, "check-bumps", "--base", "main")
    assert out.returncode == 1
    assert "already exists" in out.stdout


# --- untagged ----------------------------------------------------------------

def test_untagged_lists_packages_before_plugins(repo: Path) -> None:
    rows = run(repo, "untagged").stdout.splitlines()
    assert [r.split("\t")[3] for r in rows] == ["pkg-tool--v0.1.0", "thing--v1.0.0"]


def test_untagged_skips_released_versions(repo: Path) -> None:
    sh(repo, "git", "tag", "pkg-tool--v0.1.0")
    sh(repo, "git", "tag", "thing--v1.0.0")
    # Not even a newline: the release workflow reads each line as a release.
    assert run(repo, "untagged").stdout == ""


# --- pins and report ---------------------------------------------------------

def release_history(repo: Path) -> None:
    """thing 1.0.0 pins tool 0.1.0; thing 1.1.0 pins tool 0.2.0; main moves on."""
    sh(repo, "git", "tag", "thing--v1.0.0")
    set_pin(repo, "0.2.0")
    set_plugin_version(repo, "1.1.0")
    commit(repo, "adopt tool 0.2.0")
    sh(repo, "git", "tag", "thing--v1.1.0")
    write(repo, "plugins/thing/README.md", "unrelated\n")
    set_plugin_version(repo, "1.1.1")
    commit(repo, "docs")


def test_pins_include_supported_tags(repo: Path) -> None:
    release_history(repo)
    rows = dict(line.split("\t")[:2] for line in run(repo, "pins", "--tags", "2").stdout.splitlines())
    assert rows[PIN.format(v="0.1.0")] == "thing--v1.0.0"
    assert rows[PIN.format(v="0.2.0")] == "thing--v1.1.0 thing@HEAD"


def test_pins_window_drops_old_tags(repo: Path) -> None:
    release_history(repo)
    out = run(repo, "pins", "--tags", "1").stdout
    assert PIN.format(v="0.1.0") not in out


def test_tags_sort_by_version_not_text(repo: Path) -> None:
    # 1.10.0 sorts before 1.9.0 as text; the window must still keep 1.10.0.
    for v in ("1.9.0", "1.10.0"):
        set_plugin_version(repo, v)
        set_pin(repo, v)
        commit(repo, v)
        sh(repo, "git", "tag", f"thing--v{v}")
    out = run(repo, "pins", "--tags", "1").stdout
    assert "pkg-tool--v1.10.0" in out and "pkg-tool--v1.9.0" not in out


def spec_for(repo: Path, *args: str) -> str:
    [line] = run(repo, "pins", "--tags", "0", *args).stdout.splitlines()
    return line.split("\t")[2]


def test_pin_to_tag_this_commit_will_cut_installs_from_commit(repo: Path) -> None:
    # The first release of a package: the skill pins pkg-tool--v0.1.0, which the
    # release workflow cuts only after this check passes. Insisting on the tag
    # would deadlock; the commit holds the same files.
    assert spec_for(repo, "--at", "abc123") == PIN.format(v="0.1.0").replace(
        "@pkg-tool--v0.1.0#", "@abc123#")


def test_pin_to_existing_tag_installs_from_tag(repo: Path) -> None:
    sh(repo, "git", "tag", "pkg-tool--v0.1.0")
    assert spec_for(repo, "--at", "abc123") == PIN.format(v="0.1.0")


def test_pin_to_a_tag_no_release_will_cut_is_left_to_fail(repo: Path) -> None:
    # A typo, or a version nobody bumped the package to: not this commit's release.
    set_pin(repo, "0.9.9")
    assert spec_for(repo, "--at", "abc123") == PIN.format(v="0.9.9")


def test_pin_without_at_is_unchanged(repo: Path) -> None:
    assert spec_for(repo) == PIN.format(v="0.1.0")


def test_pin_with_extras_keeps_them(repo: Path) -> None:
    # CI must install `tool[fast] @ git+...`, not the bare URL, or a broken extra
    # passes CI and fails for every user.
    write(repo, "plugins/thing/skills/thing/SKILL.md",
          f'uvx --from "tool[fast] @ {PIN.format(v="0.1.0")}" tool\n')
    sh(repo, "git", "tag", "pkg-tool--v0.1.0")
    assert spec_for(repo) == f"tool[fast] @ {PIN.format(v='0.1.0')}"
    assert "| `tool` | `pkg-tool--v0.1.0` |" in run(repo, "report").stdout


def test_extras_pin_to_tag_this_commit_will_cut(repo: Path) -> None:
    write(repo, "plugins/thing/skills/thing/SKILL.md",
          f'uvx --from "tool[fast] @ {PIN.format(v="0.1.0")}" tool\n')
    assert spec_for(repo, "--at", "abc123") == f"tool[fast] @ {PIN.format(v='0.1.0')}".replace(
        "@pkg-tool--v0.1.0#", "@abc123#")


def test_report_check_detects_stale_file(repo: Path) -> None:
    write(repo, "docs/pins.md", run(repo, "report").stdout)
    assert run(repo, "report", "--check", "docs/pins.md").returncode == 0
    set_pin(repo, "0.3.0")
    out = run(repo, "report", "--check", "docs/pins.md")
    assert out.returncode == 1 and "stale" in out.stdout


def test_report_with_tags_shows_each_release(repo: Path) -> None:
    release_history(repo)
    out = run(repo, "report", "--tags", "2").stdout
    assert "| `thing` | 1.0.0 | `tool` | `pkg-tool--v0.1.0` |" in out
    assert "| `thing` | 1.1.0 | `tool` | `pkg-tool--v0.2.0` |" in out
    assert "| `thing` | 1.1.1 (main) | `tool` | `pkg-tool--v0.2.0` |" in out


# --- cli ---------------------------------------------------------------------

def test_cli_reads_console_script_not_directory(repo: Path) -> None:
    write(repo, "packages/tool/pyproject.toml",
          '[project]\nname = "tool"\nversion = "0.1.0"\n'
          '[project.scripts]\nt = "tool.cli:main"\n')
    out = run(repo, "cli", "tool")
    assert out.returncode == 0 and out.stdout == "t\n"


def test_cli_at_reads_a_package_since_removed(repo: Path) -> None:
    # An old plugin tag can pin a package that has since left the repo.
    write(repo, "packages/gone/pyproject.toml",
          '[project]\nname = "gone"\nversion = "0.1.0"\n'
          '[project.scripts]\ngone = "gone.cli:main"\n')
    commit(repo, "add gone")
    sh(repo, "git", "tag", "old")
    shutil.rmtree(repo / "packages/gone")
    commit(repo, "remove gone")
    assert run(repo, "cli", "gone").returncode != 0
    out = run(repo, "cli", "gone", "--at", "old")
    assert out.returncode == 0 and out.stdout == "gone\n"


def test_cli_at_missing_ref_falls_back_to_head(repo: Path) -> None:
    # A tag this commit's release will cut does not exist yet.
    write(repo, "packages/tool/pyproject.toml",
          '[project]\nname = "tool"\nversion = "0.1.0"\n'
          '[project.scripts]\nt = "tool.cli:main"\n')
    out = run(repo, "cli", "tool", "--at", "pkg-tool--v9.9.9")
    assert out.returncode == 0 and out.stdout == "t\n"


def test_cli_without_console_script_fails(repo: Path) -> None:
    out = run(repo, "cli", "tool")
    assert out.returncode != 0 and "exactly one console script" in out.stderr


def test_cli_prefers_script_named_after_directory(repo: Path) -> None:
    # packages/onto-query installs `onto-query` and its alias `oq`.
    write(repo, "packages/tool/pyproject.toml",
          '[project]\nname = "tool"\nversion = "0.1.0"\n'
          '[project.scripts]\nt = "tool.cli:main"\ntool = "tool.cli:main"\n')
    out = run(repo, "cli", "tool")
    assert out.returncode == 0 and out.stdout == "tool\n"


def test_cli_with_several_scripts_none_named_after_directory_fails(repo: Path) -> None:
    write(repo, "packages/tool/pyproject.toml",
          '[project]\nname = "tool"\nversion = "0.1.0"\n'
          '[project.scripts]\na = "tool.cli:main"\nb = "tool.cli:main"\n')
    out = run(repo, "cli", "tool")
    assert out.returncode != 0 and "['a', 'b']" in out.stderr
