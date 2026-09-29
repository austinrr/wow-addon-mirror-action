#!/usr/bin/env python3
"""Mirror a CurseForge World of Warcraft addon into the current GitHub repository.

Every CurseForge file becomes:
  * one commit on the current branch containing the unpacked addon folders
    (for managers such as lemonup that install the default branch),
  * a tag named after the version the author set in the addon's TOC,
  * a GitHub release carrying the original zip and a release.json
    (for managers such as WowUp that read releases).

Only the Python standard library, git and the gh CLI are used, all of which are
preinstalled on GitHub-hosted runners.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

CURSEFORGE_API = "https://www.curseforge.com/api/v1"
USER_AGENT = "wow-addon-mirror-action (+https://github.com/austinrr/wow-addon-mirror-action)"
PAGE_SIZE = 50
MAX_PAGES = 20

RELEASE_TYPES = {"release": 1, "beta": 2, "alpha": 3}
FILE_ID_MARKER = re.compile(r"<!-- curseforge-file-id: (\d+) -->")

# Interface ranges used when the TOC gives no better hint. Anything outside
# these ranges (60000+ and all modern six-digit interfaces) is retail.
DEFAULT_FLAVOR_RANGES = [
    (10000, 19999, "classic"),
    (20000, 29999, "bcc"),
    (30000, 39999, "wrath"),
    (40000, 49999, "cata"),
    (50000, 59999, "mists"),
]
FLAVOR_ORDER = ["mainline", "mists", "cata", "wrath", "bcc", "classic"]

BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"


class MirrorError(Exception):
    pass


# --------------------------------------------------------------------------- logging


def log(message: str) -> None:
    print(message, flush=True)


def notice(message: str) -> None:
    log(f"::notice::{message}")


def warning(message: str) -> None:
    log(f"::warning::{message}")


# --------------------------------------------------------------------------- config


@dataclass
class Config:
    project_id: int
    release_types: set[int]
    game_version_type_id: int | None
    backfill: int
    flavor_ranges: list[tuple[int, int, str]]
    keepalive: bool
    dry_run: bool
    repository: str

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "Config":
        def get(name: str, default: str = "") -> str:
            return env.get(f"INPUT_{name}", default).strip()

        project_id = get("PROJECT_ID")
        if not project_id.isdigit():
            raise MirrorError(f"project-id must be a numeric CurseForge project ID, got '{project_id}'")

        release_types = set()
        for name in re.split(r"[\s,]+", get("RELEASE_TYPES", "release").lower()):
            if not name:
                continue
            if name not in RELEASE_TYPES:
                raise MirrorError(f"unknown release type '{name}' (expected release, beta or alpha)")
            release_types.add(RELEASE_TYPES[name])

        game_version_type_id = get("GAME_VERSION_TYPE_ID")
        if game_version_type_id and not game_version_type_id.isdigit():
            raise MirrorError(f"game-version-type-id must be numeric, got '{game_version_type_id}'")

        backfill = get("BACKFILL", "10")
        if not backfill.isdigit() or int(backfill) < 1:
            raise MirrorError(f"backfill must be a positive integer, got '{backfill}'")

        repository = get("REPOSITORY") or env.get("GITHUB_REPOSITORY", "")
        if not repository:
            raise MirrorError("could not determine the target repository (GITHUB_REPOSITORY is unset)")

        return cls(
            project_id=int(project_id),
            release_types=release_types or {RELEASE_TYPES["release"]},
            game_version_type_id=int(game_version_type_id) if game_version_type_id else None,
            backfill=int(backfill),
            flavor_ranges=parse_flavor_map(get("FLAVOR_MAP")),
            keepalive=get("KEEPALIVE", "true").lower() == "true",
            dry_run=get("DRY_RUN", "false").lower() == "true",
            repository=repository,
        )


def parse_flavor_map(text: str) -> list[tuple[int, int, str]]:
    """Parse overrides like "16001=classic" or "16000-16999=classic"."""
    ranges = []
    for entry in re.split(r"[,\n]+", text):
        entry = entry.strip()
        if not entry:
            continue
        match = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?\s*=\s*([A-Za-z]+)", entry)
        if not match:
            raise MirrorError(f"invalid flavor-map entry '{entry}' (expected 'INTERFACE=flavor' or 'MIN-MAX=flavor')")
        low = int(match.group(1))
        high = int(match.group(2) or low)
        ranges.append((low, high, match.group(3).lower()))
    return ranges


def flavor_for(interface: int, overrides: list[tuple[int, int, str]]) -> str:
    for low, high, flavor in overrides + DEFAULT_FLAVOR_RANGES:
        if low <= interface <= high:
            return flavor
    return "mainline"


# --------------------------------------------------------------------------- http


def http_get(url: str, accept: str = "application/json", attempts: int = 3) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError) as error:
            if attempt == attempts:
                raise MirrorError(f"GET {url} failed: {error}") from error
            time.sleep(2**attempt)
    raise AssertionError("unreachable")


# --------------------------------------------------------------------------- curseforge


def fetch_files(config: Config, mirrored_ids: set[int]) -> list[dict]:
    """Return matching CurseForge files, newest first.

    Paging stops once a file we already mirrored shows up, or once enough
    matching files are collected to satisfy a first-run backfill.
    """
    files: list[dict] = []
    for page in range(MAX_PAGES):
        url = (
            f"{CURSEFORGE_API}/mods/{config.project_id}/files"
            f"?pageIndex={page}&pageSize={PAGE_SIZE}&sort=dateCreated&sortDescending=true"
        )
        payload = json.loads(http_get(url))
        data = payload.get("data") or []
        for item in data:
            if item.get("releaseType") not in config.release_types:
                continue
            if config.game_version_type_id and config.game_version_type_id not in (item.get("gameVersionTypeIds") or []):
                continue
            files.append(item)
        if any(item["id"] in mirrored_ids for item in files) or len(files) > config.backfill:
            break
        if len(data) < PAGE_SIZE:
            break
    return files


def select_pending(files: list[dict], mirrored_ids: set[int], backfill: int) -> tuple[list[dict], int]:
    """Pick files newer than the newest mirrored one, oldest first.

    Returns the files to mirror and how many newer-than-mirrored files were
    dropped because of the backfill limit. Files older than the newest mirrored
    file are never mirrored, so the branch history only ever moves forward.
    """
    newer = []
    for item in files:
        if item["id"] in mirrored_ids:
            break
        newer.append(item)
    selected = newer[:backfill]
    return list(reversed(selected)), len(newer) - len(selected)


def download_url(project_id: int, file_id: int) -> str:
    return f"{CURSEFORGE_API}/mods/{project_id}/files/{file_id}/download"


# --------------------------------------------------------------------------- addon package


@dataclass
class Toc:
    path: str
    fields: dict[str, str]

    @property
    def folder(self) -> str:
        return self.path.split("/", 1)[0]

    @property
    def interfaces(self) -> list[int]:
        return [int(value) for value in re.findall(r"\d+", self.fields.get("interface", ""))]


@dataclass
class Package:
    folders: list[str]
    tocs: list[Toc]
    primary: str
    stray_files: list[str] = field(default_factory=list)

    @property
    def primary_tocs(self) -> list[Toc]:
        """TOCs of the primary folder, with <Folder>.toc first."""
        tocs = [toc for toc in self.tocs if toc.folder == self.primary]
        return sorted(tocs, key=lambda toc: (PurePosixPath(toc.path).stem.lower() != self.primary.lower(), toc.path))

    @property
    def title(self) -> str:
        for toc in self.primary_tocs:
            title = strip_ui_escapes(toc.fields.get("title", ""))
            if title:
                return title
        return self.primary


def parse_toc(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.lstrip("﻿").splitlines():
        match = re.match(r"^##\s*([^:]+?)\s*:\s*(.*?)\s*$", line)
        if match:
            fields.setdefault(match.group(1).lower(), match.group(2))
    return fields


def strip_ui_escapes(text: str) -> str:
    text = re.sub(r"\|c[0-9a-fA-F]{8}|\|r", "", text)
    text = re.sub(r"\|T.*?\|t", "", text)
    return text.strip()


def is_placeholder(value: str) -> bool:
    value = value.strip()
    return not value or (value.startswith("@") and value.endswith("@"))


def normalize_name(value: str) -> str:
    return re.sub(r"[\s_\-]+", "", value).lower()


def zip_member_parts(name: str) -> tuple[str, ...] | None:
    """Split a zip member name, rejecting anything that could escape the target."""
    path = PurePosixPath(name.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        raise MirrorError(f"refusing to extract unsafe zip entry '{name}'")
    parts = path.parts
    if not parts or parts[0] == "__MACOSX" or parts[0].startswith("."):
        return None
    return parts


def read_package(zip_path: Path, file_name: str) -> Package:
    folders: set[str] = set()
    tocs: list[Toc] = []
    stray: list[str] = []
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            parts = zip_member_parts(info.filename)
            if parts is None:
                continue
            if len(parts) == 1:
                if not info.is_dir():
                    stray.append(info.filename)
                else:
                    folders.add(parts[0])
                continue
            folders.add(parts[0])
            if len(parts) == 2 and parts[1].lower().endswith(".toc") and not info.is_dir():
                text = archive.read(info).decode("utf-8", errors="replace")
                tocs.append(Toc(path="/".join(parts), fields=parse_toc(text)))

    addon_folders = sorted({toc.folder for toc in tocs})
    if not addon_folders:
        raise MirrorError(f"{file_name} contains no addon folders with a .toc file")

    # Prefer the folder named like the zip ("ZoneLevelForever-1.3.2.zip" ->
    # "ZoneLevelForever"), otherwise the shortest name, which is usually the
    # core addon that the others ("Foo_Options", "Foo_Data") extend.
    zip_prefix = normalize_name(re.split(r"[-.]", file_name, maxsplit=1)[0])
    named = [folder for folder in addon_folders if normalize_name(folder) == zip_prefix]
    primary = named[0] if named else min(addon_folders, key=lambda folder: (len(folder), folder))
    return Package(folders=sorted(folders), tocs=tocs, primary=primary, stray_files=stray)


def resolve_version(package: Package, display_name: str) -> str:
    """The version the author set in the TOC, falling back to the CurseForge display name."""
    for toc in package.primary_tocs:
        version = toc.fields.get("version", "")
        if not is_placeholder(version):
            if version != display_name:
                notice(f"TOC version '{version}' differs from CurseForge display name '{display_name}'; using the TOC version")
            return version
    warning(f"no usable '## Version' in {package.primary} TOC files; using CurseForge display name '{display_name}'")
    return display_name


def tag_name(version: str) -> str:
    """Turn a version into a valid git tag, changing as little as possible.

    Besides characters git rejects, "#" is replaced too: git allows it, but it
    truncates GitHub release and download URLs (everything after it is a fragment).
    """
    tag = re.sub(r"[\x00-\x20\x7f~^:?*\[\\#]+", "-", version.strip())
    tag = tag.replace("@{", "-")
    tag = re.sub(r"\.{2,}", ".", tag)
    tag = re.sub(r"/{2,}", "/", tag)
    tag = tag.strip("-./")
    if tag.endswith(".lock"):
        tag = tag[: -len(".lock")]
    if not tag or tag == "@":
        raise MirrorError(f"cannot derive a git tag from version '{version}'")
    result = subprocess.run(["git", "check-ref-format", f"refs/tags/{tag}"], capture_output=True)
    if result.returncode != 0:
        raise MirrorError(f"version '{version}' does not produce a valid git tag ('{tag}')")
    return tag


def build_release_json(package: Package, version: str, file_name: str, flavor_ranges) -> dict:
    by_flavor: dict[str, int] = {}
    for toc in package.primary_tocs:
        for interface in toc.interfaces:
            flavor = flavor_for(interface, flavor_ranges)
            by_flavor[flavor] = max(interface, by_flavor.get(flavor, 0))
    if not by_flavor:
        warning(f"no '## Interface' found in {package.primary} TOC files; release.json will have no flavor metadata")

    def order(flavor: str) -> tuple[int, str]:
        return (FLAVOR_ORDER.index(flavor) if flavor in FLAVOR_ORDER else len(FLAVOR_ORDER), flavor)

    return {
        "releases": [
            {
                "name": package.title,
                "version": version,
                "filename": file_name,
                "nolib": False,
                "metadata": [{"flavor": flavor, "interface": by_flavor[flavor]} for flavor in sorted(by_flavor, key=order)],
            }
        ]
    }


# --------------------------------------------------------------------------- git


def git(*args: str, env: dict[str, str] | None = None, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise MirrorError(f"git {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result.stdout


def remote_tags(cwd: Path) -> set[str]:
    tags = set()
    for line in git("ls-remote", "--tags", "origin", cwd=cwd).splitlines():
        ref = line.split("\t", 1)[1]
        tags.add(ref.removeprefix("refs/tags/").removesuffix("^{}"))
    return tags


def has_root_toc(folder: Path) -> bool:
    return any(child.is_file() and child.suffix.lower() == ".toc" for child in folder.iterdir())


def replace_addon_folders(repo: Path, zip_path: Path, package: Package) -> list[str]:
    """Swap the addon folders at the repository root for the ones in the zip.

    Returns every top-level path that was removed or written, for staging.
    """
    touched = set(package.folders)
    for child in repo.iterdir():
        if not child.is_dir() or child.name.startswith("."):
            continue
        if child.name in package.folders or has_root_toc(child):
            shutil.rmtree(child)
            touched.add(child.name)

    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            parts = zip_member_parts(info.filename)
            if parts is None or len(parts) < 2:
                continue
            target = repo.joinpath(*parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, open(target, "wb") as destination:
                shutil.copyfileobj(source, destination)
    return sorted(touched)


def commit_and_tag(repo: Path, touched: list[str], message: str, date: str, tag: str) -> None:
    for name in touched:
        if (repo / name).exists():
            git("add", "-A", "--", name, cwd=repo)
        else:
            git("rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", name, cwd=repo)
    identity = ["-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}"]
    dates = {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    # --allow-empty keeps one commit per version even if a re-upload is byte-identical,
    # so every tag points at its own commit.
    git(*identity, "commit", "-q", "--allow-empty", "-m", message, env=dates, cwd=repo)
    git("tag", tag, cwd=repo)


def push(repo: Path, branch: str, tag: str) -> None:
    git("push", "-q", "--atomic", "origin", f"HEAD:refs/heads/{branch}", f"refs/tags/{tag}", cwd=repo)


# --------------------------------------------------------------------------- github


class GitHub:
    def __init__(self, repository: str):
        self.repository = repository

    def _gh(self, *args: str) -> str:
        result = subprocess.run(["gh", *args], capture_output=True, text=True)
        if result.returncode != 0:
            raise MirrorError(f"gh {' '.join(args[:3])} failed:\n{result.stderr.strip()}")
        return result.stdout

    def releases(self) -> dict[str, str]:
        """Map of release tag -> release notes."""
        output = self._gh(
            "api", "--paginate", f"repos/{self.repository}/releases?per_page=100",
            "--jq", '.[] | {tag: .tag_name, body: (.body // "")}',
        )
        releases: dict[str, str] = {}
        for line in output.splitlines():
            if line.strip():
                release = json.loads(line)
                releases[release["tag"]] = release["body"]
        return releases

    def set_release_notes(self, tag: str, notes: str) -> None:
        self._gh("release", "edit", tag, "--repo", self.repository, "--notes", notes)

    def create_release(self, tag: str, title: str, notes: str, assets: list[Path], prerelease: bool, latest: bool) -> None:
        args = [
            "release", "create", tag, *map(str, assets),
            "--repo", self.repository, "--title", title, "--notes", notes,
            "--verify-tag", f"--latest={'true' if latest else 'false'}",
        ]
        if prerelease:
            args.append("--prerelease")
        self._gh(*args)

    def enable_workflow(self, workflow_file: str) -> None:
        self._gh("api", "-X", "PUT", f"repos/{self.repository}/actions/workflows/{workflow_file}/enable")


def release_notes(config: Config, item: dict, version: str) -> str:
    uploaded = item.get("dateCreated", "")[:10]
    game_versions = ", ".join(item.get("gameVersions") or []) or "unknown"
    return "\n".join(
        [
            f"Mirrored from [CurseForge](https://www.curseforge.com/projects/{config.project_id}).",
            "",
            "| | |",
            "|---|---|",
            f"| Version | `{version}` |",
            f"| File | `{item['fileName']}` |",
            f"| Uploaded | {uploaded} |",
            f"| Game versions | {game_versions} |",
            "",
            file_id_marker(item["id"]),
        ]
    )


def file_id_marker(file_id: int) -> str:
    return f"<!-- curseforge-file-id: {file_id} -->"


# --------------------------------------------------------------------------- main


def current_branch(repo: Path) -> str:
    try:
        return git("symbolic-ref", "--short", "HEAD", cwd=repo).strip()
    except MirrorError as error:
        raise MirrorError("the repository is in a detached HEAD state; check out a branch first") from error


def workflow_file_from_ref(ref: str) -> str | None:
    # e.g. "owner/repo/.github/workflows/mirror.yml@refs/heads/main"
    match = re.search(r"\.github/workflows/([^@]+)@", ref)
    return match.group(1) if match else None


def write_outputs(env: dict[str, str], mirrored: list[str], summary: list[str]) -> None:
    if output_path := env.get("GITHUB_OUTPUT"):
        with open(output_path, "a") as handle:
            handle.write(f"versions={json.dumps(mirrored)}\n")
            handle.write(f"latest-version={mirrored[-1] if mirrored else ''}\n")
    if summary_path := env.get("GITHUB_STEP_SUMMARY"):
        with open(summary_path, "a") as handle:
            handle.write("\n".join(summary) + "\n")


def run(config: Config, repo: Path, github: GitHub, env: dict[str, str]) -> list[str]:
    releases = github.releases()
    mirrored_ids = {int(file_id) for body in releases.values() for file_id in FILE_ID_MARKER.findall(body)}
    log(f"{len(releases)} existing release(s), {len(mirrored_ids)} mirrored from CurseForge")

    files = fetch_files(config, mirrored_ids)
    if not files:
        raise MirrorError(f"no matching CurseForge files found for project {config.project_id}")
    pending, dropped = select_pending(files, mirrored_ids, config.backfill)
    if dropped:
        warning(f"{dropped} older CurseForge file(s) skipped because of backfill={config.backfill}")
    if not pending:
        log(f"Up to date with CurseForge file {files[0]['id']} ({files[0]['displayName']})")

    newest_id = files[0]["id"]
    branch = "" if config.dry_run else current_branch(repo)
    tags = remote_tags(repo)
    mirrored: list[str] = []
    summary = ["### CurseForge mirror", ""]

    with tempfile.TemporaryDirectory(prefix="wow-addon-mirror-") as temp:
        for item in pending:
            log(f"::group::CurseForge file {item['id']}: {item['fileName']}")
            work = Path(temp) / str(item["id"])
            work.mkdir()
            zip_path = work / item["fileName"]
            zip_path.write_bytes(http_get(download_url(config.project_id, item["id"]), accept="*/*"))

            package = read_package(zip_path, item["fileName"])
            if package.stray_files:
                warning(f"ignoring files outside addon folders: {', '.join(package.stray_files)}")
            version = resolve_version(package, item["displayName"])
            tag = tag_name(version)
            log(f"addon '{package.title}' version '{version}' -> tag '{tag}', folders {package.folders}")

            if tag in releases:
                warning(f"release '{tag}' already exists; skipping CurseForge file {item['id']} (duplicate version upload?)")
                if not config.dry_run:
                    # Record the file ID so later runs treat this upload as handled.
                    releases[tag] = f"{releases[tag].rstrip()}\n{file_id_marker(item['id'])}"
                    github.set_release_notes(tag, releases[tag])
                log("::endgroup::")
                continue

            release_json_path = work / "release.json"
            release_json = build_release_json(package, version, item["fileName"], config.flavor_ranges)
            release_json_path.write_text(json.dumps(release_json, indent=2) + "\n")
            log(f"release.json:\n{release_json_path.read_text()}")

            prerelease = item.get("releaseType") != RELEASE_TYPES["release"]
            latest = item["id"] == newest_id and not prerelease

            if config.dry_run:
                action = "create release on existing tag" if tag in tags else "commit, tag and release"
                log(f"[dry-run] would {action} '{tag}' (latest={latest}, prerelease={prerelease})")
            else:
                if tag in tags:
                    notice(f"tag '{tag}' already exists without a release; creating the release for it")
                else:
                    touched = replace_addon_folders(repo, zip_path, package)
                    message = f"{package.title} {version}\n\nMirrored from CurseForge file {item['id']} ({item['fileName']})."
                    commit_and_tag(repo, touched, message, item["dateCreated"], tag)
                    push(repo, branch, tag)
                    tags.add(tag)
                github.create_release(
                    tag, version, release_notes(config, item, version),
                    [zip_path, release_json_path], prerelease=prerelease, latest=latest,
                )
                notice(f"mirrored {package.title} {version}")

            releases[tag] = file_id_marker(item["id"])
            mirrored.append(version)
            summary.append(f"- {'[dry-run] ' if config.dry_run else ''}`{version}` from `{item['fileName']}`")
            log("::endgroup::")

    if not mirrored:
        summary.append("Already up to date.")

    if config.keepalive and not config.dry_run:
        workflow_file = workflow_file_from_ref(env.get("GITHUB_WORKFLOW_REF", ""))
        if workflow_file:
            try:
                github.enable_workflow(workflow_file)
            except MirrorError as error:
                warning(f"keepalive failed (does the workflow grant 'actions: write'?): {error}")

    write_outputs(env, mirrored, summary)
    return mirrored


def main() -> int:
    env = dict(os.environ)
    try:
        config = Config.from_env(env)
        repo = Path(env.get("GITHUB_WORKSPACE") or ".").resolve()
        run(config, repo, GitHub(config.repository), env)
    except MirrorError as error:
        log(f"::error::{error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
