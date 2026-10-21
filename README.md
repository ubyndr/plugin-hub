# plugin-hub

Every Claude Code plugin we manage, from every plugin repo, in one place — and a
`plugins.toml` per project that pins which of them it uses, at which release.

- **Browse:** a catalogue marketplace listing every plugin in the repos named in
  [`sources.toml`](sources.toml), viewable in Claude Code's `/plugin` screen.
- **Pin:** `plugins.toml` in each project, like `pyproject.toml`. The `plugin-hub`
  command turns it into files Claude Code reads, and installs exactly that.

Two projects on one machine can use different releases of the same plugin, and a
teammate's whole setup after cloning is one command.

## Run it

Nothing to install; `uvx` fetches it on first use:

```sh
alias plugin-hub='uvx --from "git+https://github.com/ubyndr/plugin-hub@pkg-plugin-hub--v0.1.0#subdirectory=packages/plugin-hub" plugin-hub'
```

Needs `git`, [uv](https://docs.astral.sh/uv/), and Claude Code for `sync`.

## See what is available

```sh
plugin-hub list           # every plugin and its newest release
plugin-hub list -l        # with descriptions
```

Or browse inside Claude Code, after adding the catalogue once:

```sh
claude plugin marketplace add ubyndr/plugin-hub
```

Then `/plugin` lists everything. **Browse there, but install with `plugin-hub`.**
Installing from the catalogue gives an unpinned plugin, shared by every project
on your machine.

## Set up a project

From the project's root:

```sh
plugin-hub add cap-tools remote-h5ad-obs     # newest releases
plugin-hub add onto-mapping@0.5.0            # or a specific one
plugin-hub sync                              # install them
git add plugins.toml .claude .gitignore && git commit -m "Pin Claude Code plugins"
```

`plugins.toml` is the file you read and edit:

```toml
[project]
name = "cap-report-hacking"

[plugins]
cap-tools = "0.4.1"
remote-h5ad-obs = "0.3.0"
```

A plugin with no release tags can be pinned to a commit instead, and a plugin
outside the catalogue by its repo:

```toml
some-plugin = { sha = "22a10275db2768304021549cd8300bc05a704b42" }
other = { repo = "someone/their-plugins", path = "plugins/other", version = "1.2.0" }
```

After editing it by hand, run `plugin-hub generate`.

## After cloning, or pulling a change to plugins.toml

```sh
plugin-hub sync
```

Then restart Claude Code in the project. `sync` needs no network access to the
catalogue: everything it installs is already written in the committed files.

## Keep up to date

```sh
plugin-hub outdated          # pins with a newer release
plugin-hub upgrade           # move every pin to the newest release
plugin-hub upgrade cap-tools # or just one
plugin-hub remove cap-tools  # sync then uninstalls it here
```

Each of these rewrites the generated files; run `sync`, then commit.

## What it writes, and why

| File | What it is |
|---|---|
| `plugins.toml` | what you edit |
| `.claude/plugin-hub/.claude-plugin/marketplace.json` | the project's own marketplace, `<project>-plugins`: one entry per plugin, its `source` pinned to the release tag |
| `.claude/plugin-hub/<project>-set/` | a bundle plugin that depends on every pinned plugin, so one install brings in all of them |
| `.claude/settings.json` | declares the marketplace and enables its plugins; other settings are left alone |
| `.gitignore` | gains `.claude/settings.local.json`, where `sync` records this machine's path |

Why a marketplace per project: Claude Code keeps **one registration per
marketplace name per machine**. Pinning by putting a `ref` on a shared
marketplace (`cap_skills`, say) means every project on the machine gets whichever
pin registered last. A marketplace named after the project is that project's
alone.

Why pins on the entries and not version ranges on the bundle: Claude Code clones
a marketplace without its tags, so a range like `~0.4.0` has nothing to resolve
against and silently installs the newest release. (Claude Code 2.1.305.)

Why `add` refuses a bundle such as `atlas-tools`: its dependencies name their
own marketplace, so they would install from it unpinned and shared. Add the
plugins it bundles instead; `add` names them.

## The catalogue

`.claude-plugin/marketplace.json` is generated from the repos in
`sources.toml`, by the `catalogue` workflow — daily, on any change to
`sources.toml`, and on demand:

```sh
gh workflow run catalogue.yml
```

Each source repo's own `marketplace.json` is the source of truth; a plugin listed
there by relative path becomes a `git-subdir` entry pointing back into its repo.
Entries carry no version — releases are read from each repo's
`<plugin>--v<version>` tags when `list`, `add` or `outdated` asks — so a release
anywhere shows up at once, and the catalogue only changes when a plugin is added,
removed, renamed or re-described. Two repos with a plugin of the same name fail
the rebuild, and the old catalogue stays.

To add a plugin repo: add a `[[source]]` to `sources.toml`. For its plugins to be
pinnable by version, the repo must tag releases as `<plugin>--v<version>` — what
`claude plugin tag` writes.

## Developing

```sh
./dev.sh
```

The package versions and releases like the plugin repos: bump
`packages/plugin-hub/pyproject.toml` and `__version__` together, merge, and the
`release` workflow tags `pkg-plugin-hub--v<version>`. Then move the version in
the alias above.
