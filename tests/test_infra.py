"""Tests for the three halves of a piece that are not files or processes:
what it serves on the web, what it writes to disk, and what it repeats.

These are the parts that today are done by hand on somebody's server and then
forgotten: an nginx location, a folder with no ceiling, a cron entry. What is
checked here is that they arrive declared, that they can be taken out again,
and that the two rules that cost real time on a real machine are kept: the
exact route wins over the prefix, and a cache never throws away what people use.
"""

import argparse
import json
import os
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import package  # noqa: E402
import runtime  # noqa: E402
import w4ve  # noqa: E402


def a_server(tmp):
    root = Path(tmp) / "root"
    (root / "server" / "mods").mkdir(parents=True)
    (root / "w4ve").mkdir()
    (root / "w4ve.toml").write_text(
        '[server]\nkind = "server"\nmc = "1.21"\ngame_dir = "server"\n\n[pieces]\n')
    return w4ve.Server(root)


def build_package(folder, data, files=()):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "w4ve.json").write_text(json.dumps(data, indent=2))
    for relative, content in files:
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return package.pack(folder, folder.parent)[0]


FULL = {
    "schema": 1, "id": "demo", "version": "1.0.0", "side": "server",
    "summary": "A piece with every second half there is.",
    "settings": [
        {"name": "public_url", "type": "url", "required": True,
         "help": "Where clients reach it."},
    ],
    "guide": ["Point a DNS record at this machine.", "Then: w4ve run"],
    "components": [
        {"kind": "service", "name": "demo-api", "command": ["true"],
         "health": {"http": "http://127.0.0.1:9/health"}},
        {"kind": "http", "name": "demo-api", "path": "/v1/upload",
         "exact": True, "proxy": "127.0.0.1:8792", "max_body": "8m",
         "why": "uploads from the game"},
        {"kind": "http", "name": "demo-media", "path": "/v1/media/",
         "serve": "w4ve/storage/demo/media", "cache": "immutable"},
        {"kind": "storage", "name": "demo-media",
         "dir": "w4ve/storage/demo/media", "quota": "1G", "keep": "7d"},
        {"kind": "timer", "name": "demo-sync", "command": ["true"],
         "every": "60s", "why": "publish new emotes"},
    ],
}


class DeclaringCase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="w4ve-infra-"))
        self.server = a_server(self.tmp)

    def install(self, data=FULL, files=()):
        pkg = build_package(self.tmp / ("src-" + data["id"]), data, files)
        install = package.Install(pkg, self.server)
        actions, problems = install.plan()
        self.assertEqual(problems, [])
        record = install.apply(actions)
        install.commit()
        return record

    def test_every_second_half_lands_in_the_toml(self):
        self.install()
        text = (self.server.root / "w4ve.toml").read_text()
        for block in ("[services.demo-api]", "[http.demo-api]",
                      "[http.demo-media]", "[storage.demo-media]",
                      "[timer.demo-sync]"):
            self.assertIn(block, text)
        self.assertIn("# installed by demo", text)

    def test_a_storage_folder_is_made_at_install_not_at_first_write(self):
        self.install()
        self.assertTrue((self.server.root / "w4ve/storage/demo/media").is_dir())

    def test_removing_takes_out_what_it_put_in_and_leaves_the_data(self):
        self.install()
        (self.server.root / "w4ve/storage/demo/media/keep.png").write_text("x")
        result = package.remove(self.server, "demo")
        text = (self.server.root / "w4ve.toml").read_text()
        for block in ("[services.demo-api]", "[http.demo-api]",
                      "[storage.demo-media]", "[timer.demo-sync]"):
            self.assertNotIn(block, text)
        self.assertIn("w4ve/storage/demo/media", result["kept_data"])
        self.assertTrue(
            (self.server.root / "w4ve/storage/demo/media/keep.png").exists(),
            "removing a piece must never delete somebody's data")

    def test_removing_forgets_the_settings_so_a_reinstall_asks_again(self):
        self.install()
        declarations = w4ve.declarations_for(self.server, "demo")
        self.server.set_setting("demo", "public_url", "https://x.example/",
                                declarations, "demo")
        package.remove(self.server, "demo")
        self.assertNotIn("[settings.demo]",
                         (self.server.root / "w4ve.toml").read_text())

    def test_a_killed_install_takes_its_declarations_with_it(self):
        pkg = build_package(self.tmp / "src-boom", FULL)
        install = package.Install(pkg, self.server)
        actions, _ = install.plan()
        # Die after the toml has been written to, which is the case that used
        # to leave a service block behind pointing at nothing.
        with self.assertRaises(RuntimeError):
            def boom(*_args, **_kwargs):
                raise RuntimeError("killed")
            install._add_timer = boom
            install.apply(actions)
        text = (self.server.root / "w4ve.toml").read_text()
        self.assertNotIn("[services.demo-api]", text)
        self.assertNotIn("[http.demo-api]", text)

    def test_the_guide_is_kept_so_it_can_be_printed_later(self):
        record = self.install()
        self.assertEqual(record["guide"][1], "Then: w4ve run")


class ClientJarCase(unittest.TestCase):
    """The half of a piece that the players download."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="w4ve-client-"))
        self.server = a_server(self.tmp)
        data = dict(FULL, id="chat", settings=[], components=[
            {"kind": "client", "file": "client/chat-1.0.0+1.21.jar",
             "mc": "1.21"},
            {"kind": "client", "file": "client/chat-1.0.0+1.21.4.jar",
             "mc": "1.21.4"},
            {"kind": "http", "name": "chat-dl", "path": "/dl/",
             "serve": "w4ve/client", "cache": "short"},
        ])
        pkg = build_package(self.tmp / "src-chat", data,
                            [("client/chat-1.0.0+1.21.jar", "jar one"),
                             ("client/chat-1.0.0+1.21.4.jar", "jar two")])
        install = package.Install(pkg, self.server)
        actions, problems = install.plan()
        self.assertEqual(problems, [])
        self.record = install.apply(actions)
        install.commit()

    def test_every_client_jar_is_kept_not_only_the_one_for_this_server(self):
        # The server runs one Minecraft version; the players do not all run
        # the same one, so all of them are served.
        folder = self.server.root / "w4ve" / "client"
        self.assertTrue((folder / "chat-1.0.0+1.21.jar").exists())
        self.assertTrue((folder / "chat-1.0.0+1.21.4.jar").exists())

    def test_the_index_is_written_from_what_was_installed(self):
        index = json.loads(
            (self.server.root / "w4ve" / "client" / "latest.json").read_text())
        self.assertEqual(index["version"], "1.0.0")
        self.assertEqual(index["minecraft"]["1.21"]["file"],
                         "chat-1.0.0+1.21.jar")
        self.assertEqual(len(index["minecraft"]["1.21"]["sha256"]), 64)

    def test_the_hash_in_the_index_is_the_hash_of_the_jar_beside_it(self):
        folder = self.server.root / "w4ve" / "client"
        index = json.loads((folder / "latest.json").read_text())
        self.assertEqual(index["minecraft"]["1.21.4"]["sha256"],
                         package.sha256_of(folder / "chat-1.0.0+1.21.4.jar"))


class ValidationCase(unittest.TestCase):

    def problems(self, component):
        data = dict(FULL, components=[component])
        return package.Manifest(data).problems()

    def test_a_route_is_either_a_folder_or_a_service(self):
        self.assertTrue(any("one of the two" in p for p in self.problems(
            {"kind": "http", "name": "x", "path": "/a", "serve": "w4ve/a",
             "proxy": "127.0.0.1:1"})))
        self.assertTrue(any("one of the two" in p for p in self.problems(
            {"kind": "http", "name": "x", "path": "/a"})))

    def test_a_route_cannot_serve_something_outside_the_server(self):
        self.assertTrue(any("escapes" in p for p in self.problems(
            {"kind": "http", "name": "x", "path": "/a", "serve": "/etc"})))

    def test_a_storage_with_no_ceiling_is_just_a_folder(self):
        self.assertTrue(any("just a folder" in p for p in self.problems(
            {"kind": "storage", "name": "x", "dir": "w4ve/x"})))

    def test_a_timer_faster_than_five_seconds_is_a_loop(self):
        self.assertTrue(any("loop and not a timer" in p for p in self.problems(
            {"kind": "timer", "name": "x", "command": ["true"], "every": "1s"})))


class NginxCase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="w4ve-nginx-"))
        self.server = a_server(self.tmp)
        pkg = build_package(self.tmp / "src", FULL)
        install = package.Install(pkg, self.server)
        actions, _ = install.plan()
        install.apply(actions)
        install.commit()

    def test_the_exact_route_is_written_before_the_prefix(self):
        routes = w4ve.http_routes(self.server)
        self.assertEqual(routes[0]["path"], "/v1/upload")
        text = w4ve.render_nginx(self.server, routes)
        self.assertLess(text.index("location = /v1/upload"),
                        text.index("location /v1/media/"))

    def test_the_things_that_cost_a_night_are_in_there(self):
        text = w4ve.render_nginx(self.server, w4ve.http_routes(self.server))
        self.assertIn("client_max_body_size 8m;", text)
        self.assertIn("immutable", text)      # its own cache header
        self.assertIn("autoindex off;", text)
        self.assertIn("proxy_pass http://127.0.0.1:8792/v1/upload;", text)

    def test_an_alias_ends_in_a_slash_because_nginx_needs_it_to(self):
        text = w4ve.render_nginx(self.server, w4ve.http_routes(self.server))
        line = [l for l in text.splitlines() if l.strip().startswith("alias")][0]
        self.assertTrue(line.rstrip().endswith("/;"), line)

    def test_caddy_is_offered_because_it_does_the_certificate_itself(self):
        text = w4ve.render_caddy(self.server, w4ve.http_routes(self.server))
        self.assertIn("reverse_proxy 127.0.0.1:8792", text)
        self.assertIn("file_server", text)


class PurgeCase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="w4ve-purge-"))
        self.folder = self.tmp / "media"
        self.folder.mkdir()

    def write(self, name, size, age_seconds, accessed=None):
        path = self.folder / name
        path.write_bytes(b"x" * size)
        moment = time.time() - age_seconds
        access = time.time() - (accessed if accessed is not None else age_seconds)
        os.utime(path, (access, moment))
        return path

    def test_what_is_older_than_the_rule_goes(self):
        old = self.write("old.png", 10, 8 * 86400)
        new = self.write("new.png", 10, 60)
        freed, gone = runtime.purge_area(self.folder, None, 7 * 86400, "mtime")
        self.assertEqual(gone, 1)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())

    def test_a_quota_takes_the_oldest_until_it_fits(self):
        for index in range(5):
            self.write("f%d.bin" % index, 100, 5000 - index * 100)
        freed, gone = runtime.purge_area(self.folder, 300, None, "mtime")
        self.assertEqual(gone, 2)
        left = sorted(p.name for p in self.folder.iterdir())
        self.assertEqual(left, ["f2.bin", "f3.bin", "f4.bin"])

    def test_a_cache_keeps_what_people_use_even_when_it_is_the_oldest(self):
        # Written long ago, played today. `by = "atime"` is the difference
        # between a cache and a shredder.
        played = self.write("favourite.ogg", 100, 90 * 86400, accessed=10)
        forgotten = self.write("never.ogg", 100, 2 * 86400, accessed=2 * 86400)
        runtime.purge_area(self.folder, 150, None, "atime")
        self.assertTrue(played.exists())
        self.assertFalse(forgotten.exists())

    def test_usage_counts_what_is_really_there(self):
        self.write("a.bin", 1000, 10)
        self.write("b.bin", 2000, 10)
        used, count = runtime.folder_usage(self.folder)
        self.assertEqual((used, count), (3000, 2))


class TimerCase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="w4ve-timer-"))
        self.root = Path(self.tmp) / "root"
        (self.root / "w4ve").mkdir(parents=True)
        self.guardian = runtime.Guardian(self.root)

    def test_a_timer_that_works_says_nothing_and_counts(self):
        timer = self.guardian.add_timer("ok", ["true"], 60)
        self.guardian._run_timer(timer)
        self.assertEqual(timer["runs"], 1)
        self.assertEqual(timer["streak"], 0)

    def test_a_timer_that_fails_says_so_once_and_keeps_count(self):
        timer = self.guardian.add_timer("bad", ["false"], 60)
        for _ in range(3):
            self.guardian._run_timer(timer)
        self.assertEqual(timer["failures"], 3)
        self.assertEqual(timer["streak"], 3)
        journal = (self.root / "w4ve" / "run" / "journal.log").read_text()
        # Once, not three times: a timer failing every minute would bury the
        # journal and teach everybody to ignore it.
        self.assertEqual(journal.count("timer bad failed"), 1)

    def test_a_timer_that_recovers_says_that_too(self):
        timer = self.guardian.add_timer("flaky", ["false"], 60)
        self.guardian._run_timer(timer)
        timer["command"] = ["true"]
        self.guardian._run_timer(timer)
        journal = (self.root / "w4ve" / "run" / "journal.log").read_text()
        self.assertIn("is working again", journal)

    def test_a_command_that_does_not_exist_is_not_a_crash(self):
        timer = self.guardian.add_timer("nope", ["/nonexistent/x"], 60)
        self.guardian._run_timer(timer)
        self.assertEqual(timer["failures"], 1)
        self.assertIn("could not run",
                      (self.root / "w4ve" / "run" / "journal.log").read_text())


class WiringCase(unittest.TestCase):
    """What the guardian is handed once the answers exist."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="w4ve-wire-"))
        self.server = a_server(self.tmp)
        data = dict(FULL, components=[
            {"kind": "service", "name": "demo-api", "command": ["true"],
             "env": {"PUBLIC_URL": "${setting:public_url}",
                     "TOKEN": "${secret:discord_token}"}},
            {"kind": "storage", "name": "demo-media",
             "dir": "w4ve/storage/demo/media", "quota": "1G"},
            {"kind": "timer", "name": "demo-sync", "command": ["true"],
             "every": "60s"},
        ], settings=[
            {"name": "public_url", "type": "url", "required": True,
             "help": "Where clients reach it."},
            {"name": "discord_token", "type": "secret", "required": True,
             "help": "Bot tab."},
        ])
        pkg = build_package(self.tmp / "src", data)
        install = package.Install(pkg, self.server)
        actions, _ = install.plan()
        install.apply(actions)
        install.commit()

    def answer(self):
        declarations = w4ve.declarations_for(self.server, "demo")
        self.server.set_setting("demo", "public_url", "https://x.example/",
                                declarations, "demo")
        self.server.set_setting("demo", "discord_token", "hunter2",
                                declarations, "demo")
        self.server = w4ve.Server(self.server.root)

    def test_the_service_gets_the_answers_in_its_environment(self):
        self.answer()
        guardian, _ = w4ve.runtime_pieces(self.server, runtime)
        env = guardian.watched["demo-api"].env
        self.assertEqual(env["PUBLIC_URL"], "https://x.example/")
        self.assertEqual(env["TOKEN"], "hunter2")
        # And without anybody writing a template: the process can just read
        # its own environment.
        self.assertEqual(env["W4VE_PUBLIC_URL"], "https://x.example/")
        self.assertEqual(env["W4VE_PIECE"], "demo")

    def test_a_service_with_a_hole_where_its_token_goes_is_not_started(self):
        guardian, _ = w4ve.runtime_pieces(self.server, runtime)
        self.assertNotIn("demo-api", guardian.watched,
                         "starting with an empty token fails unreadably later")

    def test_timers_and_storage_reach_the_guardian(self):
        self.answer()
        guardian, _ = w4ve.runtime_pieces(self.server, runtime)
        self.assertIn("demo-sync", guardian.timers)
        self.assertEqual(guardian.timers["demo-sync"]["every"], 60)
        self.assertIn("demo-media", guardian.storage)

    def test_who_owns_a_block_comes_from_what_install_wrote_down(self):
        self.assertEqual(w4ve.owner_of(self.server, "services", "demo-api"),
                         "demo")
        self.assertEqual(w4ve.owner_of(self.server, "timer", "demo-sync"),
                         "demo")


class ReadingCase(unittest.TestCase):

    def test_the_toml_gives_back_what_was_written_into_it(self):
        tmp = Path(tempfile.mkdtemp(prefix="w4ve-read-"))
        server = a_server(tmp)
        server.set_timer("sync", {"command": ["echo", "hi"], "every": "60s",
                                  "why": "because"}, owner="demo")
        server.set_storage("media", {"dir": "w4ve/storage/media",
                                     "quota": "2G", "keep": "7d"}, owner="demo")
        again = w4ve.Server(server.root)
        timers = runtime.timers_from(again.config)
        self.assertEqual(timers[0]["command"], ["echo", "hi"])
        self.assertEqual(timers[0]["every"], 60)
        areas = runtime.storage_from(again.config)
        self.assertEqual(areas[0]["quota"], "2G")

    def test_an_env_table_survives_being_written_and_read(self):
        # `env = {"A": "b"}` is an inline table, and the small TOML reader for
        # Python 3.9 used to hand it back as the literal string.
        tmp = Path(tempfile.mkdtemp(prefix="w4ve-env-"))
        server = a_server(tmp)
        server.set_service("api", {"command": ["true"], "cwd": ".",
                                   "restart": "on-failure",
                                   "env": {"A": "b"}}, owner="demo")
        text = (server.root / "w4ve.toml").read_text()
        parsed = w4ve._toml_lite(text)
        self.assertEqual(parsed["services"]["api"]["env"], {"A": "b"})


if __name__ == "__main__":
    unittest.main()


class UpdateKeepsAnswersCase(unittest.TestCase):
    """Updating a piece must never lose what somebody answered.

    A token is typed once and forgotten. An update that quietly wipes it looks
    exactly like the piece breaking by itself, and the person who could fix it
    no longer has the value.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="w4ve-upd-"))
        self.server = a_server(self.tmp)
        self.data = dict(FULL, components=[
            {"kind": "service", "name": "demo-api", "command": ["true"]},
        ], settings=[
            {"name": "public_url", "type": "url", "required": True,
             "help": "Where clients reach it."},
            {"name": "token", "type": "secret", "required": True,
             "help": "The secret."},
        ])

    def install(self, version):
        data = dict(self.data, version=version)
        pkg = build_package(self.tmp / ("src-" + version), data)
        install = package.Install(pkg, self.server)
        actions, problems = install.plan()
        self.assertEqual(problems, [])
        install.apply(actions)
        install.commit()
        self.server = w4ve.Server(self.server.root)

    def test_an_update_keeps_the_settings_and_the_secret(self):
        self.install("1.0.0")
        declarations = w4ve.declarations_for(self.server, "demo")
        self.server.set_setting("demo", "public_url", "https://x.example/",
                                declarations, "demo")
        self.server.set_setting("demo", "token", "hunter2", declarations, "demo")
        self.server = w4ve.Server(self.server.root)

        package.remove(self.server, "demo", keep_settings=True)
        self.install("1.1.0")

        self.assertEqual(self.server.plain_settings("demo")["public_url"],
                         "https://x.example/")
        self.assertEqual(self.server.secrets_of("demo")["token"], "hunter2")
        self.assertEqual(w4ve.missing_settings(self.server, "demo"), [])

    def test_a_real_removal_still_forgets_them(self):
        self.install("1.0.0")
        declarations = w4ve.declarations_for(self.server, "demo")
        self.server.set_setting("demo", "token", "hunter2", declarations, "demo")
        package.remove(self.server, "demo")
        self.assertEqual(self.server.secrets_of("demo"), {})


class CatalogPackageCase(unittest.TestCase):
    """A package the catalog offers, which is what makes it one command.

    Without this the catalog can only hand you the half of a piece that
    happens to be a jar, and installing the other half means finding a file
    somewhere and typing its path. The whole format exists to close that gap.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="w4ve-cat-"))
        self.server = a_server(self.tmp)
        data = dict(FULL, id="demo", version="1.0.0", settings=[], components=[
            {"kind": "service", "name": "demo-api", "command": ["true"]},
            {"kind": "storage", "name": "demo-media",
             "dir": "w4ve/storage/demo/media", "quota": "1G"},
        ])
        self.pkg = build_package(self.tmp / "src", data)
        digest = package.sha256_of(self.pkg)
        self.index = {
            "schema": 1, "profiles": {},
            "pieces": {"demo": {
                "id": "demo", "name": "Demo", "type": "w4ve-package",
                "side": "server", "summary": "s", "mc": "*",
                "releases": [{"version": "1.0.0", "source": "github", "files": [
                    {"name": self.pkg.name, "url": self.pkg.as_uri(),
                     "hashes": {"sha256": digest}}]}],
            }},
        }
        self.args = argparse.Namespace(dry_run=False, force=False, yes=True,
                                       dir=str(self.server.root))

    def test_a_package_from_the_catalog_installs_like_any_other(self):
        changed, message = w4ve.install_one(self.server, self.index, "demo",
                                            quiet=True)
        self.assertTrue(changed, message)
        again = w4ve.Server(self.server.root)
        self.assertIn("[services.demo-api]",
                      (again.root / "w4ve.toml").read_text())
        self.assertEqual(again.state["pieces"]["demo"]["source"], "package")

    def test_the_package_file_is_not_left_lying_around(self):
        w4ve.install_one(self.server, self.index, "demo", quiet=True)
        leftovers = list(Path(self.server.root).rglob("*.w4ve"))
        self.assertEqual(leftovers, [],
                         "a delivery format kept on disk is one more thing "
                         "nobody knows whether they can delete")

    def test_installing_the_same_version_twice_does_nothing(self):
        w4ve.install_one(self.server, self.index, "demo", quiet=True)
        again = w4ve.Server(self.server.root)
        changed, _ = w4ve.install_one(again, self.index, "demo", quiet=True)
        self.assertFalse(changed)

    def test_a_dry_run_says_what_it_would_do_and_writes_nothing(self):
        changed, message = w4ve.install_one(self.server, self.index, "demo",
                                            dry_run=True, quiet=True)
        self.assertTrue(changed)
        self.assertIn("package", message)
        self.assertNotIn("[services.demo-api]",
                         (self.server.root / "w4ve.toml").read_text())
