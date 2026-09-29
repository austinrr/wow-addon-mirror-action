# wow-addon-mirror-action

A GitHub Action that mirrors a World of Warcraft addon from CurseForge into a GitHub repository, so it can be installed by addon managers that only support GitHub.

Each new CurseForge file becomes:

- **a commit on the default branch** containing the unpacked addon folders. [lemonup](https://github.com/archcorsair/lemonup) installs from the branch head.
- **a tag** named after the version the author set in the addon's TOC (`## Version:`), such as `1.3.2`. Mirrors never restart at `v1`.
- **a GitHub release** with the original CurseForge zip and a `release.json`. [WowUp](https://wowup.io) reads releases.

It only needs Python's standard library, `git` and the `gh` CLI, all preinstalled on GitHub-hosted runners, so there is no setup step. A run with nothing new takes a few seconds.

## Usage

Create a repository for the mirror and add `.github/workflows/mirror.yml`:

```yaml
name: Mirror from CurseForge

on:
  schedule:
    - cron: "17 */2 * * *"
  workflow_dispatch:

permissions:
  contents: write   # push commits/tags, create releases
  actions: write    # keepalive

concurrency: mirror

jobs:
  mirror:
    runs-on: ubuntu-latest
    steps:
      - uses: austinrr/wow-addon-mirror-action@v1
        with:
          project-id: 1703102
```

The project ID is shown under **About Project** on the addon's CurseForge page. The action checks out the repository itself.

The first run backfills the newest `backfill` versions, oldest first. Later runs mirror only what's new.

## Inputs

| Input | Default | Description |
|---|---|---|
| `project-id` | required | CurseForge project ID. |
| `release-types` | `release` | Comma-separated: `release`, `beta`, `alpha`. Betas and alphas become prereleases and are never marked latest. |
| `game-version-type-id` | | Only mirror files tagged with this CurseForge game version type. Use this for addons that upload a separate zip per game flavor. |
| `backfill` | `10` | Maximum number of new versions created in one run. |
| `flavor-map` | | Overrides for `release.json` flavors, e.g. `16001=classic` or `16000-16999=classic`. |
| `keepalive` | `true` | Re-enables the workflow through the API so GitHub doesn't disable the schedule after 60 days without commits. It never makes commits. Needs `actions: write`. |
| `dry-run` | `false` | Download and inspect files and log what would happen, without committing, pushing or creating releases. |
| `token` | `github.token` | Token used for git pushes and the GitHub API. |

## Outputs

| Output | Description |
|---|---|
| `versions` | JSON array of versions mirrored in this run, oldest first. |
| `latest-version` | Newest version mirrored in this run, or empty. |

## How it works

**What counts as new.** Each release's notes include a hidden `<!-- curseforge-file-id: N -->` marker, so CurseForge files are matched to releases by ID without downloading anything. Only files newer than the newest mirrored one are processed, so the branch history only ever moves forward.

**Version and tag.** The version comes from `## Version:` in the addon's main TOC. If it's missing or still an unreplaced `@project-version@` placeholder, the CurseForge display name is used instead. The tag is that version with characters git or GitHub URLs can't handle (spaces, `:`, `#`, `..`, …) replaced. The release title and `release.json` keep the exact string.

**Repository contents.** Addon folders (top-level folders containing a `.toc`) are replaced on every update. Everything else, such as README, LICENSE, `.github/` and other folders, is left alone. Commits carry the CurseForge upload date.

**`release.json`.** Built from every TOC in the main addon folder. Each `## Interface:` number (comma lists included) is mapped to a flavor by range: `1xxxx` classic, `2xxxx` bcc, `3xxxx` wrath, `4xxxx` cata, `5xxxx` mists, anything newer mainline. The highest interface per flavor wins.

```json
{
  "releases": [{
    "name": "Zone Level: Forever",
    "version": "1.3.2",
    "filename": "ZoneLevelForever-1.3.2.zip",
    "nolib": false,
    "metadata": [{ "flavor": "classic", "interface": 16001 }]
  }]
}
```

**Recovery.** If a run pushes a tag but fails before creating its release, the next run creates the release for the existing tag. If an author re-uploads a version that already has a release, the upload is recorded on that release and skipped.

## Notes

- lemonup treats any new commit on the default branch as an update. Editing the README or workflow makes it re-download the same version once. That's harmless, but keep such edits rare.
- CurseForge data comes from the public `www.curseforge.com/api/v1` endpoints, which don't need an API key but aren't officially documented.

## Development

```sh
python3 -m unittest discover -s tests -v
```

To try it against a real project without changing anything, run it from inside a clone of the mirror repository:

```sh
GH_TOKEN=$(gh auth token) INPUT_PROJECT_ID=1703102 INPUT_DRY_RUN=true \
  GITHUB_REPOSITORY=owner/mirror-repo GITHUB_WORKSPACE=$PWD \
  python3 path/to/mirror.py
```
