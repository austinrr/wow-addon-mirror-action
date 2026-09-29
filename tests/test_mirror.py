import io
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mirror  # noqa: E402


def make_zip(path: Path, files: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return path


def cf_file(file_id: int, display_name: str, release_type: int = 1, type_ids=(88568,)) -> dict:
    return {
        "id": file_id,
        "displayName": display_name,
        "fileName": f"Addon-{display_name}.zip",
        "releaseType": release_type,
        "gameVersions": ["1.60.1"],
        "gameVersionTypeIds": list(type_ids),
        "dateCreated": f"2026-09-{10 + file_id % 10:02d}T12:00:00.000Z",
    }


class TocTests(unittest.TestCase):
    def test_parse_toc_handles_bom_crlf_and_first_value_wins(self):
        fields = mirror.parse_toc("﻿## Interface: 110200, 50500\r\n## Title: Foo\r\n##Version:1.2\r\n## Title: Bar\r\n")
        self.assertEqual(fields["interface"], "110200, 50500")
        self.assertEqual(fields["title"], "Foo")
        self.assertEqual(fields["version"], "1.2")

    def test_interfaces_supports_comma_lists(self):
        toc = mirror.Toc("Foo/Foo.toc", {"interface": "110200, 50500,11507"})
        self.assertEqual(toc.interfaces, [110200, 50500, 11507])

    def test_strip_ui_escapes(self):
        self.assertEqual(mirror.strip_ui_escapes("|cff00ff00Zone|r Level |TInterface\\Icon:16|t"), "Zone Level")

    def test_placeholder(self):
        self.assertTrue(mirror.is_placeholder("@project-version@"))
        self.assertTrue(mirror.is_placeholder(" "))
        self.assertFalse(mirror.is_placeholder("1.3.2"))


class FlavorTests(unittest.TestCase):
    def test_default_ranges(self):
        cases = {11507: "classic", 16001: "classic", 20505: "bcc", 30403: "wrath", 40402: "cata",
                 50500: "mists", 110200: "mainline", 120100: "mainline"}
        for interface, flavor in cases.items():
            self.assertEqual(mirror.flavor_for(interface, []), flavor, interface)

    def test_overrides_take_precedence(self):
        overrides = mirror.parse_flavor_map("16001=forever\n 16100 - 16199 = other")
        self.assertEqual(mirror.flavor_for(16001, overrides), "forever")
        self.assertEqual(mirror.flavor_for(16150, overrides), "other")
        self.assertEqual(mirror.flavor_for(11507, overrides), "classic")

    def test_invalid_flavor_map(self):
        with self.assertRaises(mirror.MirrorError):
            mirror.parse_flavor_map("classic")


class TagTests(unittest.TestCase):
    def test_simple_versions_are_unchanged(self):
        for version in ["1.3.2", "v2.0.0-beta1", "Details.20260918.15280.172", "12.0.0/release"]:
            self.assertEqual(mirror.tag_name(version), version)

    def test_invalid_characters_are_replaced(self):
        self.assertEqual(mirror.tag_name("1.0 (hotfix)"), "1.0-(hotfix)")
        self.assertEqual(mirror.tag_name("r1..2"), "r1.2")
        self.assertEqual(mirror.tag_name("1.0:final"), "1.0-final")
        self.assertEqual(mirror.tag_name("#Details.20260918.15280.172"), "Details.20260918.15280.172")

    def test_empty_version_fails(self):
        with self.assertRaises(mirror.MirrorError):
            mirror.tag_name("  ")


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_single_folder_addon(self):
        path = make_zip(self.dir / "ZoneLevelForever-1.3.2.zip", {
            "ZoneLevelForever/ZoneLevelForever.toc": "## Interface: 16001\n## Title: Zone Level: Forever\n## Version: 1.3.2\n",
            "ZoneLevelForever/ZoneLevelForever.lua": "-- code",
        })
        package = mirror.read_package(path, path.name)
        self.assertEqual(package.primary, "ZoneLevelForever")
        self.assertEqual(package.title, "Zone Level: Forever")
        self.assertEqual(mirror.resolve_version(package, "1.3.2"), "1.3.2")
        release = mirror.build_release_json(package, "1.3.2", path.name, [])
        self.assertEqual(release, {"releases": [{
            "name": "Zone Level: Forever", "version": "1.3.2", "filename": path.name, "nolib": False,
            "metadata": [{"flavor": "classic", "interface": 16001}],
        }]})

    def test_multi_folder_multi_toc_addon(self):
        path = make_zip(self.dir / "Details-Details.20260918.15280.172.zip", {
            "Details/Details.toc": "## Interface: 120100\n## Title: Details! Damage Meter\n## Version: @project-version@\n",
            "Details/Details_Classic.toc": "## Interface: 11508, 50501\n## Title: Details! Damage Meter\n",
            "Details_DataStorage/Details_DataStorage.toc": "## Interface: 120100\n## Version: 9\n",
            "Details_TinyThreat/Details_TinyThreat.toc": "## Interface: 120100\n",
            "Details/Libs/LibStub.lua": "",
        })
        package = mirror.read_package(path, path.name)
        self.assertEqual(package.primary, "Details")
        self.assertEqual(package.folders, ["Details", "Details_DataStorage", "Details_TinyThreat"])
        # Placeholder version falls back to the CurseForge display name, not a sibling folder's TOC.
        self.assertEqual(mirror.resolve_version(package, "Details.20260918.15280.172"), "Details.20260918.15280.172")
        metadata = mirror.build_release_json(package, "x", path.name, [])["releases"][0]["metadata"]
        self.assertEqual(metadata, [
            {"flavor": "mainline", "interface": 120100},
            {"flavor": "mists", "interface": 50501},
            {"flavor": "classic", "interface": 11508},
        ])

    def test_zip_without_toc_fails(self):
        path = make_zip(self.dir / "Broken-1.0.zip", {"Broken/readme.txt": "hi"})
        with self.assertRaises(mirror.MirrorError):
            mirror.read_package(path, path.name)

    def test_unsafe_zip_entry_fails(self):
        path = make_zip(self.dir / "Evil-1.0.zip", {"Evil/Evil.toc": "## Interface: 1", "../escape.txt": "x"})
        with self.assertRaises(mirror.MirrorError):
            mirror.read_package(path, path.name)

    def test_replace_addon_folders_keeps_repo_files(self):
        repo = self.dir / "repo"
        (repo / "OldAddon").mkdir(parents=True)
        (repo / "OldAddon" / "OldAddon.toc").write_text("## Interface: 1")
        (repo / ".github" / "workflows").mkdir(parents=True)
        (repo / "docs").mkdir()
        (repo / "README.md").write_text("readme")
        path = make_zip(self.dir / "New-1.0.zip", {"New/New.toc": "## Interface: 1", "New/sub/a.lua": "a"})
        touched = mirror.replace_addon_folders(repo, path, mirror.read_package(path, path.name))
        self.assertEqual(touched, ["New", "OldAddon"])
        self.assertFalse((repo / "OldAddon").exists())
        self.assertEqual((repo / "New" / "sub" / "a.lua").read_text(), "a")
        self.assertTrue((repo / "README.md").exists())
        self.assertTrue((repo / "docs").exists())
        self.assertTrue((repo / ".github" / "workflows").exists())


class SelectionTests(unittest.TestCase):
    def test_first_run_takes_newest_backfill_oldest_first(self):
        files = [cf_file(i, f"1.{i}") for i in range(7, 0, -1)]
        pending, dropped = mirror.select_pending(files, set(), backfill=3)
        self.assertEqual([f["id"] for f in pending], [5, 6, 7])
        self.assertEqual(dropped, 4)

    def test_only_files_newer_than_newest_mirrored(self):
        files = [cf_file(i, f"1.{i}") for i in range(7, 0, -1)]
        pending, dropped = mirror.select_pending(files, {5, 2}, backfill=10)
        self.assertEqual([f["id"] for f in pending], [6, 7])
        self.assertEqual(dropped, 0)

    def test_workflow_file_from_ref(self):
        ref = "austinrr/zone-level-mirror/.github/workflows/mirror.yml@refs/heads/main"
        self.assertEqual(mirror.workflow_file_from_ref(ref), "mirror.yml")
        self.assertIsNone(mirror.workflow_file_from_ref(""))


class FakeGitHub:
    def __init__(self, releases=None):
        self.release_bodies = dict(releases or {})
        self.created = []
        self.edited = []
        self.enabled = []

    def releases(self):
        return dict(self.release_bodies)

    def create_release(self, tag, title, notes, assets, prerelease, latest):
        self.created.append({"tag": tag, "title": title, "latest": latest, "prerelease": prerelease,
                             "assets": [p.name for p in assets],
                             "release_json": json.loads(assets[1].read_text())})
        self.release_bodies[tag] = notes

    def set_release_notes(self, tag, notes):
        self.edited.append(tag)
        self.release_bodies[tag] = notes

    def enable_workflow(self, workflow_file):
        self.enabled.append(workflow_file)


class EndToEndTests(unittest.TestCase):
    """Runs the full flow against a local bare repo with CurseForge and GitHub stubbed out."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.remote = root / "remote.git"
        self.repo = root / "work"
        run = lambda *args, cwd=root: subprocess.run(args, cwd=cwd, check=True, capture_output=True)
        run("git", "init", "-q", "--bare", "-b", "main", str(self.remote))
        run("git", "clone", "-q", str(self.remote), str(self.repo))
        (self.repo / "README.md").write_text("mirror\n")
        run("git", "add", "README.md", cwd=self.repo)
        run("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init", cwd=self.repo)
        run("git", "push", "-q", "origin", "main", cwd=self.repo)
        self.zips = {}
        self.files = []

    def tearDown(self):
        self.temp.cleanup()

    def add_upload(self, file_id, version, toc_version=None, release_type=1):
        item = cf_file(file_id, version, release_type)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("Addon/Addon.toc", f"## Interface: 16001\n## Title: Addon\n## Version: {toc_version or version}\n")
            archive.writestr("Addon/Addon.lua", f"-- {version}")
        self.zips[file_id] = buffer.getvalue()
        self.files.insert(0, item)

    def fake_http_get(self, url, accept="application/json", attempts=3):
        if url.endswith("/download"):
            return self.zips[int(url.split("/")[-2])]
        return json.dumps({"data": self.files}).encode()

    def run_mirror(self, github, **overrides):
        env = {"INPUT_PROJECT_ID": "1703102", "GITHUB_REPOSITORY": "o/r",
               "GITHUB_WORKFLOW_REF": "o/r/.github/workflows/mirror.yml@refs/heads/main"}
        env.update({f"INPUT_{k.upper()}": v for k, v in overrides.items()})
        config = mirror.Config.from_env(env)
        with mock.patch.object(mirror, "http_get", self.fake_http_get), mock.patch("builtins.print"):
            return mirror.run(config, self.repo, github, env)

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.remote, check=True, capture_output=True, text=True).stdout

    def test_backfill_then_incremental(self):
        for file_id, version in [(1, "1.0"), (2, "1.1"), (3, "1.2")]:
            self.add_upload(file_id, version)
        github = FakeGitHub()

        self.assertEqual(self.run_mirror(github), ["1.0", "1.1", "1.2"])
        self.assertEqual([r["tag"] for r in github.created], ["1.0", "1.1", "1.2"])
        self.assertEqual([r["latest"] for r in github.created], [False, False, True])
        self.assertEqual(github.created[-1]["assets"], ["Addon-1.2.zip", "release.json"])
        self.assertEqual(github.created[-1]["release_json"]["releases"][0]["metadata"],
                         [{"flavor": "classic", "interface": 16001}])
        self.assertEqual(github.enabled, ["mirror.yml"])

        log = self.git("log", "--format=%s|%aI", "main").splitlines()
        self.assertEqual(log[0], "Addon 1.2|2026-09-13T12:00:00Z")
        self.assertEqual(len(log), 4)
        self.assertEqual(self.git("show", "1.1:Addon/Addon.lua"), "-- 1.1")
        self.assertEqual(self.git("show", "main:README.md"), "mirror\n")

        # Nothing new: no commits, no releases.
        self.assertEqual(self.run_mirror(github), [])
        self.assertEqual(len(github.created), 3)

        # A new upload is picked up on its own.
        self.add_upload(4, "1.3")
        self.assertEqual(self.run_mirror(github), ["1.3"])
        self.assertEqual(self.git("show", "main:Addon/Addon.lua"), "-- 1.3")

    def test_duplicate_version_upload_is_recorded_not_released(self):
        self.add_upload(1, "1.0")
        github = FakeGitHub()
        self.run_mirror(github)
        self.add_upload(2, "1.0-reupload", toc_version="1.0")
        self.assertEqual(self.run_mirror(github), [])
        self.assertEqual(github.edited, ["1.0"])
        self.assertIn("curseforge-file-id: 2", github.release_bodies["1.0"])
        # Recorded, so the next run does not download it again.
        self.assertEqual(self.run_mirror(github), [])
        self.assertEqual(github.edited, ["1.0"])

    def test_existing_tag_without_release_is_repaired(self):
        self.add_upload(1, "1.0")
        github = FakeGitHub()
        self.run_mirror(github)
        del github.release_bodies["1.0"]
        before = self.git("rev-parse", "main")
        self.assertEqual(self.run_mirror(github), ["1.0"])
        self.assertEqual(self.git("rev-parse", "main"), before)
        self.assertEqual(len(github.created), 2)

    def test_prerelease_is_never_latest(self):
        self.add_upload(1, "2.0-beta", release_type=2)
        github = FakeGitHub()
        self.run_mirror(github, release_types="release,beta")
        self.assertEqual(github.created[0]["prerelease"], True)
        self.assertEqual(github.created[0]["latest"], False)

    def test_dry_run_changes_nothing(self):
        self.add_upload(1, "1.0")
        github = FakeGitHub()
        before = self.git("rev-parse", "main")
        self.assertEqual(self.run_mirror(github, dry_run="true"), ["1.0"])
        self.assertEqual(github.created, [])
        self.assertEqual(github.enabled, [])
        self.assertEqual(self.git("rev-parse", "main"), before)
        self.assertEqual(self.git("tag"), "")


if __name__ == "__main__":
    unittest.main()
