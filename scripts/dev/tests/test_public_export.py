"""Tests for the public-export rewrite engine and fail-closed gates.

No private value appears in this file: probes are obviously synthetic
(example/acme-style names, zero-entropy IDs), and any check against the real
private inventory loads that inventory from the excluded private policy at
runtime instead of spelling it. Tests needing the private policy skip when it
is absent (public CI runs only the generic suite).
"""

import ast
import codecs
import importlib.util
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve()
SCRIPT = HERE.parents[2] / "public_export.py"
CONFIG = HERE.parents[2] / "public_export_config.json"
PRIVATE = HERE.parents[2] / "public_export_private.json"
HAS_PRIVATE = PRIVATE.is_file()
TOOLING = (
    "public_export.py",
    "public_export_config.json",
    "dev/tests/test_public_export.py",
)
SHIPPED = [HERE.parents[2] / name for name in TOOLING]
spec = importlib.util.spec_from_file_location("public_export", SCRIPT)
public_export = importlib.util.module_from_spec(spec)
spec.loader.exec_module(public_export)


def endpoint(char="a", upper=False, scheme=True):
    domain = ".R2.CLOUDFLARESTORAGE.COM" if upper else ".r2.cloudflarestorage.com"
    return ("https://" if scheme else "") + char * 32 + domain


def _fold_str(node, limit=65536):
    """Best-effort constant string folder for the shipped-source gate.

    Handles string literals, + concatenation, "s" * n repetition and joined
    f-string parts. Anything non-constant (names, calls, unbounded sizes)
    yields None: unfolded code stays covered by the raw-substring gate.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                return None
        out = "".join(parts)
        return out if len(out) <= limit else None
    if isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.Add):
            left, right = _fold_str(node.left, limit), _fold_str(node.right, limit)
            if not isinstance(left, str) or not isinstance(right, str):
                return None
            out = left + right
            return out if len(out) <= limit else None
        if isinstance(node.op, ast.Mult):
            left, right = node.left, node.right
            if isinstance(left, ast.Constant) and isinstance(left.value, bool):
                return None
            if isinstance(right, ast.Constant) and isinstance(right.value, bool):
                return None
            left_s, right_s = _fold_str(left, limit), _fold_str(right, limit)
            left_n = left.value if isinstance(left, ast.Constant) else None
            right_n = right.value if isinstance(right, ast.Constant) else None
            if isinstance(left_s, str) and isinstance(right_n, int):
                num, part = right_n, left_s
            elif isinstance(right_s, str) and isinstance(left_n, int):
                num, part = left_n, right_s
            else:
                return None
            if num < 0 or len(part) * num > limit:
                return None
            return part * num
    return None


def _folded_strings(tree, limit=65536):
    """All constant string values obtainable by folding concatenations."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.BinOp, ast.JoinedStr)):
            value = _fold_str(node, limit)
            if isinstance(value, str):
                out.append(value)
    return out


def opaque_id():
    # Obviously synthetic: sequential runs, no private token inside.
    return "TestOnly000" + "00001XyZ"


def real_policy(testcase):
    if not HAS_PRIVATE:
        testcase.skipTest("private export policy absent")
    return public_export.load_policy(str(CONFIG), str(PRIVATE))


def make_repo(testcase, files, links=()):
    repo = Path(tempfile.mkdtemp(prefix="export-fixture-"))
    testcase.addCleanup(shutil.rmtree, repo, True)
    for rel, data in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    for link, dest in links:
        target = repo / link
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(dest)
    env = {"GIT_CONFIG_NOSYSTEM": "1", "HOME": str(repo)}
    subprocess.run(
        ["git", "init", "-q"], cwd=repo, check=True, env={**os.environ, **env}
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@localhost",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=repo,
        check=True,
    )
    return repo


def write_policy_files(testcase, public=None, private=None):
    tmp = Path(tempfile.mkdtemp(prefix="export-policy-"))
    testcase.addCleanup(shutil.rmtree, tmp, True)
    pub = tmp / "public.json"
    pub.write_text(
        json.dumps(public if public is not None else json.loads(CONFIG.read_text()))
    )
    priv = tmp / "private.json"
    priv.write_text(
        json.dumps(
            private
            if private is not None
            else {
                "version": 1,
                "replacements": [],
                "drop_sections": [],
                "banned_tokens": [],
                "submodule_note_path": "SUBMODULES.md",
                "workflow_path": ".github/workflows/check.yml",
            }
        )
    )
    return str(pub), str(priv)


def export_fixture(testcase, files, links=(), private=None, public=None):
    repo = make_repo(testcase, files, links)
    pub, priv = write_policy_files(testcase, public, private)
    out = Path(tempfile.mkdtemp(prefix="export-out-"))
    testcase.addCleanup(shutil.rmtree, out, True)
    summary = public_export.export_tree("HEAD", out, pub, priv, repo=repo)
    return repo, summary, out


def assert_export_fails(testcase, files, rule_id, links=(), private=None, public=None):
    repo = make_repo(testcase, files, links)
    pub, priv = write_policy_files(testcase, public, private)
    out = Path(tempfile.mkdtemp(prefix="export-out-"))
    testcase.addCleanup(shutil.rmtree, out, True)
    with testcase.assertRaises(SystemExit) as ctx:
        public_export.export_tree("HEAD", out, pub, priv, repo=repo)
    testcase.assertIn(rule_id, str(ctx.exception))
    return repo


class EngineTest(unittest.TestCase):
    def test_apply_counts_and_replaces(self):
        rules = [
            {
                "id": "x",
                "paths": ["docs/**"],
                "regex": False,
                "pattern": "alpha",
                "replacement": "beta",
            }
        ]
        text, counts = public_export.apply_text("docs/n.md", "alpha alpha", rules)
        self.assertEqual((text, counts), ("beta beta", {"x": 2}))

    def test_path_globs(self):
        rules = [
            {
                "id": "x",
                "paths": ["docs/**"],
                "regex": False,
                "pattern": "a",
                "replacement": "b",
            }
        ]
        _, counts = public_export.apply_text("scripts/n.py", "a", rules)
        self.assertEqual(counts, {})

    def test_drop_section_markers_required(self):
        with self.assertRaises(SystemExit):
            public_export.apply_drop_sections(
                "no markers here",
                [{"paths": ["**"], "start": "AAA", "end": "BBB", "replacement": "C"}],
                "n.md",
            )
        text = public_export.apply_drop_sections(
            "keep\nAAA drop this\nBBB keep\n",
            [{"paths": ["**"], "start": "AAA", "end": "BBB", "replacement": "C"}],
            "n.md",
        )
        self.assertEqual(text, "keep\nC\nBBB keep\n")

    def test_drop_section_alone_changes_payload(self):
        policy = {
            "replacements": [],
            "forbidden": [],
            "drop_sections": [
                {
                    "paths": ["docs/note.md"],
                    "start": "SYNTH-START",
                    "end": "SYNTH-END",
                    "replacement": "cut here.",
                }
            ],
            "exclude_paths": [],
            "banned_tokens": [],
        }
        files = {
            "docs/note.md": (b"keep\nSYNTH-START secret-part\nSYNTH-END keep\n", 0o644)
        }
        payload, applied, dropped = public_export.build_payload(
            files, [], [], [], None, policy
        )
        self.assertEqual(
            payload["docs/note.md"][1], b"keep\ncut here.\nSYNTH-END keep\n"
        )

    def test_decode_text_tiers(self):
        self.assertEqual(public_export.decode_text(b"plain")[1], "utf-8")
        self.assertEqual(public_export.decode_text("ü".encode("utf-16")), (None, None))
        self.assertEqual(
            public_export.decode_text("caf\xe9".encode("latin-1"))[1], "latin-1"
        )
        self.assertEqual(
            public_export.decode_text("caf\xe9".encode("latin-1"))[0][3], "\xe9"
        )
        self.assertEqual(public_export.decode_text("x".encode("utf-32")), (None, None))
        self.assertEqual(public_export.decode_text(b"\x00bin")[0], None)


class PrivacyBypassTest(unittest.TestCase):
    def test_home_path(self):
        assert_export_fails(
            self, {"n.txt": "config in /ho" + "me/acme-qa/private.txt\n"}, "f-home-dir"
        )

    def test_email(self):
        assert_export_fails(
            self, {"n.txt": "contact qa.synth" + "@acme-example-test.com\n"}, "f-email"
        )

    def test_email_no_tld(self):
        assert_export_fails(
            self, {"n.txt": "contact ops" + "@acme-host" + " here\n"}, "f-email-no-tld"
        )
        repo, summary, out = export_fixture(self, {"n.txt": "a ci@example.invalid\n"})
        self.assertIn("ci@example.invalid", (out / "n.txt").read_text())

    def test_ssh_command(self):
        assert_export_fails(
            self, {"n.txt": "deploy via ss" + "h acme-bastion-01\n"}, "f-ssh"
        )

    def test_ssh_uppercase(self):
        assert_export_fails(
            self, {"n.txt": "run SS" + "H acme-home-01\n"}, "f-ssh-noun"
        )
        assert_export_fails(
            self, {"n.txt": "$ SS" + "H acme-bastion-01\n"}, "f-ssh-upper"
        )
        repo, summary, out = export_fixture(
            self, {"n.txt": "tunnel via SSH loopback ok\n"}
        )
        self.assertIn("SSH loopback", (out / "n.txt").read_text())

    def test_ssh_prose_noun(self):
        assert_export_fails(
            self, {"n.txt": "connect via SS" + "H acme-relay-02\n"}, "f-ssh-noun"
        )
        for noun in ("loopback", "tunnel", "server", "agent"):
            repo, summary, out = export_fixture(
                self, {"n.txt": "use SS" + "H " + noun + " here\n"}
            )
            self.assertIn(noun, (out / "n.txt").read_text())

    def test_ssh_config_host(self):
        assert_export_fails(
            self, {"cfg": "Host acme-ba" + "stion\n  HostName x\n"}, "f-ssh-host"
        )
        repo, summary, out = export_fixture(
            self, {"docs/n.md": "the Host dispatcher routes it\n"}
        )
        self.assertIn("Host dispatcher", (out / "docs" / "n.md").read_text())

    def test_boat_doc_placeholders_pass(self):
        ssh_line = "just boat ss" + "h bx" + "_ID\n"
        home_line = "cd /ho" + "me/user/blockfield-workspace\n"
        exec_line = (
            "just boat exec --timeout 1800 bx" + "_ID bash -lc 'cd /ho"
            "me/user/blockfield-workspace; git status --short'\n"
        )
        repo, summary, out = export_fixture(
            self, {"docs/BOAT.md": ssh_line + home_line + exec_line}
        )
        text = (out / "docs" / "BOAT.md").read_text()
        self.assertIn("just boat ssh\n", text)
        self.assertIn("cd ~/blockfield-workspace\n", text)
        self.assertIn("cd ~/blockfield-workspace; git status --short", text)
        self.assertEqual(summary["applied"].get("boat-ssh-vm-id"), 1)
        self.assertEqual(summary["applied"].get("boat-home-workspace"), 2)

    def test_private_repo_name_becomes_public_name(self):
        # Split so this test file itself survives the export unchanged.
        line = (
            "git clone https://github.com/Blockfield/blockfield-workspace"
            + "-private.git\n"
        )
        repo, summary, out = export_fixture(self, {"README.md": line})
        text = (out / "README.md").read_text()
        self.assertEqual(
            text, "git clone https://github.com/Blockfield/blockfield-workspace.git\n"
        )
        self.assertEqual(summary["applied"].get("repo-public-name"), 1)

    def test_uppercase_endpoint_is_scrubbed(self):
        repo, summary, out = export_fixture(
            self, {"docs/n.md": endpoint(upper=True) + "\n"}
        )
        self.assertIn("<R2_ACCOUNT_ID>", (out / "docs" / "n.md").read_text())

    def test_endpoint_no_scheme_http_jurisdiction(self):
        for probe in (
            endpoint(scheme=False),
            endpoint().replace("https://", "http://"),
            endpoint().replace(".r2.", ".eu.r2."),
        ):
            repo, summary, out = export_fixture(self, {"docs/n.md": probe + "\n"})
            self.assertIn("<R2_ACCOUNT_ID>", (out / "docs" / "n.md").read_text())

    def test_bare_account_id(self):
        assert_export_fails(
            self, {"n.txt": "R2_ACCOUNT_I" + "D=" + "c" * 32 + "\n"}, "f-r2-bare-id"
        )

    def test_split_bare_account_id(self):
        split = "R2_ACCOUNT_I" + "D=" + "d" * 16 + "\n" + "d" * 16 + "\n"
        assert_export_fails(self, {"n.txt": split}, "f-r2-bare-id")

    def test_bare_account_id_json_and_yaml(self):
        key = "R2_ACCOUNT_" + "ID"
        value = "c" * 32
        cases = [
            '{"' + key + '": "' + value + '"}\n',
            key + ": " + value + "\n",
            "'" + key + "': '" + value + "'\n",
        ]
        for probe in cases:
            with self.subTest(probe=probe[:12]):
                assert_export_fails(self, {"docs/n.md": probe}, "f-r2-bare-id")

    def test_scripts_app_suffix_fails_closed(self):
        assert_export_fails(
            self,
            {"scripts/n.txt": "Dokploy `blockfield-proxy-" + "abcdef`\n"},
            "f-app-suffix",
        )

    def test_web_errors_suffixes_scrubbed(self):
        for name, stem in (
            ("blockfield-we" + "b-fedcba", "web"),
            ("blockfield-erro" + "rs-fedcba", "errors"),
        ):
            repo, summary, out = export_fixture(self, {"docs/n.md": f"app `{name}`\n"})
            self.assertIn(
                f"blockfield-{stem}-<SUFFIX>", (out / "docs" / "n.md").read_text()
            )

    def test_random_app_name(self):
        assert_export_fails(
            self,
            {"docs/n.md": "app `app-synthsvc-front" + "end-a1b2c3`\n"},
            "f-app-random",
        )

    def test_opaque_id_contexts(self):
        token = opaque_id()
        cases = [
            (f"see `{token}` here\n", "f-opaque-backtick"),
            (f'see "{token}" here\n', "f-opaque-quoted"),
            (f"key={token}\n", "f-opaque-assigned"),
            (f"url https://example.com/application/{token} ok\n", "f-opaque-bare"),
            (f"deploy {token} now\n", "f-opaque-bare"),
            (f'data {{"applicationId": "{token}"}}\n', "f-opaque-quoted"),
        ]
        for probe, rule in cases:
            with self.subTest(rule=rule):
                assert_export_fails(self, {"docs/n.md": probe}, rule)

    def test_opaque_bare_sentence_punctuation(self):
        token = "TestOnly000" + "00002XyZ"
        cases = [
            f"Deploy {token}.\n",
            f"For {token}: see notes\n",
        ]
        for probe in cases:
            with self.subTest(probe=probe[:10]):
                assert_export_fails(self, {"docs/n.md": probe}, "f-opaque-bare")

    def test_opaque_field_names_pass(self):
        repo, summary, out = export_fixture(
            self, {"docs/n.md": "fields `clientPackSha256`, `serverPackSha256`\n"}
        )
        self.assertIn("clientPackSha256", (out / "docs" / "n.md").read_text())

    def test_opaque_allowlisted_tokens_pass(self):
        repo, summary, out = export_fixture(
            self, {"docs/n.md": "pins `Deadmau5EarsLayer` and `Xvfb-linux-amd64` ok\n"}
        )
        text = (out / "docs" / "n.md").read_text()
        self.assertIn("Deadmau5EarsLayer", text)

    def test_opaque_allowed_then_forbidden_same_line(self):
        ident = "TestOnly" + "Abc123XyZ"
        cases = [
            ("see `Deadmau5EarsLayer` and `" + ident + "` now\n", "f-opaque-backtick"),
            ('see "Deadmau5EarsLayer" and "' + ident + '" now\n', "f-opaque-quoted"),
            ("deploy Deadmau5EarsLayer then " + ident + " now\n", "f-opaque-bare"),
            ("key=Deadmau5EarsLayer other=" + ident + "\n", "f-opaque-assigned"),
            (
                "url https://example.com/a/Deadmau5EarsLayer/b/" + ident + " ok\n",
                "f-opaque-bare",
            ),
        ]
        for probe, rule in cases:
            with self.subTest(rule=rule):
                assert_export_fails(self, {"docs/n.md": probe}, rule)

    def test_opaque_longer_token_containing_allowed(self):
        cases = [
            ("Xvfb-linux-amd64" + "x7Qa", "f-opaque-bare"),
            ("Qa7Xvfb-" + "linux-amd64", "f-opaque-bare"),
            ("TestOnly9" + "Subscribe" + "C2SPacket", "f-opaque-backtick"),
        ]
        for token, rule in cases:
            with self.subTest(token=token):
                assert_export_fails(self, {"docs/n.md": f"see `{token}` here\n"}, rule)

    def test_split_r2_endpoint_is_scrubbed(self):
        split = "https://" + "c" * 16 + "\n" + "c" * 16 + ".r2.cloudflarestorage.com"
        repo, summary, out = export_fixture(self, {"docs/n.md": split + "\n"})
        self.assertIn("<R2_ACCOUNT_ID>", (out / "docs" / "n.md").read_text())

    def test_submodule_url_variants(self):
        modules = (
            '[submodule "a"]\n\tpath = localmods/synth-priv\n'
            "\turl = https://github.com/Blockfield/synth-priv.git\n"
        )
        private = {
            "version": 1,
            "replacements": [],
            "drop_sections": [],
            "banned_tokens": [],
        }
        for url in (
            "https://github.com/Blockfield/synth-priv/blob/main/X.md",
            "http://github.com/Blockfield/synth-priv/blob/main/X.md",
            "github.com/Blockfield/synth-priv/blob/main/X.md",
            "https://www.github.com/Blockfield/synth-priv/blob/main/X.md",
            "HTTPS://GITHUB.COM/Blockfield/SYNTH-PRIV/blob/main/X.md",
        ):
            with self.subTest(url=url):
                repo, summary, out = export_fixture(
                    self,
                    {".gitmodules": modules, "docs/n.md": url + "\n"},
                    private=private,
                )
                text = (out / "docs" / "n.md").read_text()
                self.assertIn("<private-submodule>", text)
                self.assertNotIn("synth-priv/blob", text)
        repo, summary, out = export_fixture(
            self,
            {
                ".gitmodules": modules,
                "docs/n.md": "see https://github.com/Blockfield/other/blob/main/X.md\n",
            },
            private=private,
        )
        self.assertIn("Blockfield/other", (out / "docs" / "n.md").read_text())


class EncodingTest(unittest.TestCase):
    def test_utf16_fails_closed(self):
        # Fail closed: the pinned scanner reads bytes, so UTF-16 must never
        # pass as text. BOM-marked LE/BE and NUL-interleaved unmarked LE all
        # land in the binary hash allowlist, which rejects them.
        bodies = [
            ("see " + endpoint() + " here\n").encode("utf-16-le"),
            codecs.BOM_UTF16_LE + ("see " + endpoint() + " here\n").encode("utf-16-le"),
            codecs.BOM_UTF16_BE + ("see " + endpoint() + " here\n").encode("utf-16-be"),
        ]
        for data in bodies:
            with self.subTest(data=data[:4]):
                assert_export_fails(
                    self, {"docs/n.md": data}, "binary file not allowlisted by hash"
                )

    def test_utf16_secret_scanner_regression(self):
        # Real pinned scanner through the real entry point. The token below
        # is deterministic test data, never a credential. UTF-8 is the
        # positive control (gitleaks rule github-pat fires); UTF-16LE/BE
        # must fail closed as non-allowlisted binaries in both the export
        # and the public re-check.
        alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
        token = "ghp_" + "".join(random.Random(148006).choices(alphabet, k=36))
        line = f'github_token = "{token}"\n'
        probes = {
            "utf-8": line.encode("utf-8"),
            "utf-16-le": codecs.BOM_UTF16_LE + line.encode("utf-16-le"),
            "utf-16-be": codecs.BOM_UTF16_BE + line.encode("utf-16-be"),
        }
        for name, data in probes.items():
            with self.subTest(probe=name):
                repo = make_repo(self, {"probe.txt": data})
                pub, priv = write_policy_files(self)
                out = Path(tempfile.mkdtemp(prefix="export-scan-"))
                self.addCleanup(shutil.rmtree, out, True)
                with self.assertRaises(SystemExit) as ctx:
                    public_export.main(
                        [
                            "--ref",
                            "HEAD",
                            "--out",
                            str(out),
                            "--config",
                            pub,
                            "--private",
                            priv,
                        ],
                        repo=repo,
                    )
                if name == "utf-8":
                    self.assertIn("gitleaks gate failed", str(ctx.exception))
                else:
                    self.assertIn("binary file not allowlisted", str(ctx.exception))
                    with self.assertRaises(SystemExit) as check_ctx:
                        public_export.check_tree(repo, pub)
                    self.assertIn(
                        "binary file not allowlisted", str(check_ctx.exception)
                    )

    def test_utf32_fails_closed(self):
        data = ("see " + endpoint() + " here\n").encode("utf-32")
        assert_export_fails(
            self, {"docs/n.md": data}, "binary file not allowlisted by hash"
        )

    def test_latin1_prose_is_gated(self):
        data = ("see " + endpoint() + " caf\xe9\n").encode("latin-1")
        repo, summary, out = export_fixture(self, {"docs/n.md": data})
        raw = (out / "docs" / "n.md").read_bytes().decode("latin-1")
        self.assertIn("<R2_ACCOUNT_ID>", raw)

    def test_nonallowlisted_binary_fails_closed(self):
        assert_export_fails(
            self,
            {"bin/blob.dat": b"\x00\x01\x02binary"},
            "binary file not allowlisted by hash",
        )

    def test_allowlisted_jar_verified(self):
        jar = HERE.parents[3] / "scripts/dev/testbot/gradle/wrapper/gradle-wrapper.jar"
        if not jar.is_file():
            self.skipTest("wrapper jar absent")
        import hashlib

        digest = hashlib.sha256(jar.read_bytes()).hexdigest()
        public = json.loads(CONFIG.read_text())
        public["allowed_binaries"] = {"lib/tool.jar": digest}
        good = jar.read_bytes()
        repo, summary, out = export_fixture(self, {"lib/tool.jar": good}, public=public)
        self.assertEqual((out / "lib" / "tool.jar").read_bytes(), good)
        assert_export_fails(
            self,
            {"lib/tool.jar": good + b"tampered"},
            "binary file not allowlisted by hash",
            public=public,
        )


class ScannerSuppressionTest(unittest.TestCase):
    """Tracked scanner-suppression inputs must not silence the pinned scan.

    Synthetic tokens only (deterministic github-style PATs, never
    credentials); the real pinned scanner runs through the real entry
    point in an isolated scratch repo.
    """

    ALLOWLIST_TOML = '[extend]\nuseDefault = true\n[allowlist]\npaths = [".*"]\n'

    @staticmethod
    def synthetic_token(seed):
        alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
        return "ghp_" + "".join(random.Random(seed).choices(alphabet, k=36))

    def run_main(self, repo, out):
        pub, priv = write_policy_files(self)
        with self.assertRaises(SystemExit) as ctx:
            public_export.main(
                [
                    "--ref",
                    "HEAD",
                    "--out",
                    str(out),
                    "--config",
                    pub,
                    "--private",
                    priv,
                ],
                repo=repo,
            )
        return ctx

    def test_tracked_config_cannot_suppress(self):
        token = self.synthetic_token(148009)
        repo = make_repo(
            self,
            {
                "probe.txt": f'github_token = "{token}"\n',
                ".gitleaks.toml": self.ALLOWLIST_TOML,
            },
        )
        out = Path(tempfile.mkdtemp(prefix="export-scan-"))
        self.addCleanup(shutil.rmtree, out, True)
        ctx = self.run_main(repo, out)
        self.assertIn("gitleaks gate failed", str(ctx.exception))

    def test_inherited_config_env_cannot_suppress(self):
        token = self.synthetic_token(148009)
        repo = make_repo(self, {"probe.txt": f'github_token = "{token}"\n'})
        out = Path(tempfile.mkdtemp(prefix="export-scan-"))
        self.addCleanup(shutil.rmtree, out, True)
        scratch = Path(tempfile.mkdtemp(prefix="export-env-"))
        self.addCleanup(shutil.rmtree, scratch, True)
        env_config = scratch / "allow.toml"
        env_config.write_text(self.ALLOWLIST_TOML)
        with mock.patch.dict(
            os.environ,
            {
                "GITLEAKS_CONFIG": str(env_config),
                "GITLEAKS_CONFIG_TOML": self.ALLOWLIST_TOML,
            },
        ):
            ctx = self.run_main(repo, out)
        self.assertIn("gitleaks gate failed", str(ctx.exception))

    def test_inline_allow_comment_cannot_suppress(self):
        token = self.synthetic_token(148009)
        repo = make_repo(
            self, {"probe.txt": f'github_token = "{token}" # gitleaks:allow\n'}
        )
        out = Path(tempfile.mkdtemp(prefix="export-scan-"))
        self.addCleanup(shutil.rmtree, out, True)
        ctx = self.run_main(repo, out)
        self.assertIn("gitleaks gate failed", str(ctx.exception))

    def test_tracked_ignore_file_cannot_suppress(self):
        token = self.synthetic_token(148009)
        out = Path(tempfile.mkdtemp(prefix="export-scan-"))
        self.addCleanup(shutil.rmtree, out, True)
        fingerprint = f"{out}/probe.txt:github-pat:1"
        repo = make_repo(
            self,
            {
                "probe.txt": f'github_token = "{token}"\n',
                ".gitleaksignore": fingerprint + "\n",
            },
        )
        ctx = self.run_main(repo, out)
        self.assertIn("scanner-suppression", str(ctx.exception))


class SymlinkTest(unittest.TestCase):
    def test_external_symlink_never_touched_and_dropped(self):
        outside = Path(tempfile.mkdtemp(prefix="export-outside-"))
        self.addCleanup(shutil.rmtree, outside, True)
        probe = outside / "secret.txt"
        before = "see " + endpoint() + "\n"
        probe.write_text(before)
        repo, summary, out = export_fixture(
            self, {"docs/n.md": "clean\n"}, links=[("docs/linked.txt", str(probe))]
        )
        self.assertEqual(probe.read_text(), before)
        self.assertFalse((out / "docs" / "linked.txt").exists())
        self.assertIn("docs/linked.txt", summary["symlinks_dropped"])

    def test_absolute_and_escaping_links_dropped(self):
        repo, summary, out = export_fixture(
            self,
            {"docs/n.md": "clean\n", "sub/x.txt": "clean\n"},
            links=[
                ("docs/abs.txt", "/etc/hostname"),
                ("sub/up.txt", "../../escape.txt"),
            ],
        )
        self.assertFalse((out / "docs" / "abs.txt").exists())
        self.assertFalse((out / "sub/up.txt").exists())
        self.assertEqual(
            sorted(summary["symlinks_dropped"]), ["docs/abs.txt", "sub/up.txt"]
        )

    def test_internal_link_to_scrubbed_file_kept(self):
        repo, summary, out = export_fixture(
            self, {"docs/real.txt": "clean\n"}, links=[("docs/short.txt", "real.txt")]
        )
        link = out / "docs" / "short.txt"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.read_text(), "clean\n")

    def test_excluded_symlink_paths_dropped(self):
        repo, summary, out = export_fixture(
            self,
            {"docs/n.md": "clean\n"},
            links=[
                ("scripts/public_export_private.json", "docs/n.md"),
                (".dev/tracked.txt", "docs/n.md"),
            ],
        )
        self.assertFalse((out / "scripts" / "public_export_private.json").exists())
        self.assertFalse((out / ".dev").exists())
        self.assertEqual(
            sorted(summary["symlinks_dropped"]),
            [".dev/tracked.txt", "scripts/public_export_private.json"],
        )


class WorkflowTest(unittest.TestCase):
    def test_generated_workflow_is_public_safe(self):
        workflow = public_export.render_workflow()
        self.assertIn("runs-on: ubuntu-24.04", workflow)
        self.assertNotIn("self-hosted", workflow)
        self.assertNotIn("pull_request_target", workflow)
        self.assertNotIn("secrets.", workflow)
        self.assertIn("test_public_export", workflow)
        self.assertIn("--check .", workflow)
        self.assertIn("--redact", workflow)
        self.assertIn("--ignore-gitleaks-allow", workflow)
        self.assertIn("--gitleaks-ignore-path", workflow)
        self.assertIn("--config /tmp/gitleaks-trusted.toml", workflow)
        self.assertIn("[extend]", workflow)
        for pin in (
            public_export.WORKFLOW_PINS["checkout"],
            public_export.WORKFLOW_PINS["setup-python"],
        ):
            self.assertRegex(pin, r"\A[0-9a-f]{40}\Z")
            self.assertIn(pin, workflow)

    def test_generated_workflow_passes_actionlint(self):
        workflow = public_export.render_workflow()
        # The printf template must keep literal backslash-n so the run block
        # stays one indented YAML scalar (unescaped \n broke parsing before).
        self.assertIn("printf '[extend]\\nuseDefault = true\\n'", workflow)
        self.assertIn("unset GITLEAKS_CONFIG GITLEAKS_CONFIG_TOML", workflow)
        actionlint = shutil.which("actionlint")
        if actionlint is None and not HAS_PRIVATE:
            # Public CI has no pinned toolchain; the private CI keeps this mandatory.
            self.skipTest("actionlint not on PATH")
        self.assertIsNotNone(actionlint, "actionlint not on PATH")
        with tempfile.TemporaryDirectory() as tmp:
            wf = Path(tmp) / "public-export-check.yml"
            wf.write_text(workflow)
            result = subprocess.run(
                [actionlint, "-no-color", str(wf)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        self.assertEqual(result.returncode, 0, msg=result.stdout)

    def test_source_workflows_and_dev_dropped(self):
        future = (
            "on: pull_request_target\njobs:\n  x:\n    runs-on: self-hosted\n"
            "    steps:\n      - run: echo ${{ secrets.TOKEN }}\n"
        )
        repo, summary, out = export_fixture(
            self,
            {
                ".dev/tracked.txt": "x\n",
                ".github/workflows/future.yml": future,
                ".github/workflows/legacy.yml": "on: push\njobs: {}\n",
            },
        )
        workflows = sorted(
            p.relative_to(out).as_posix()
            for p in (out / ".github" / "workflows").glob("*")
        )
        self.assertEqual(workflows, [".github/workflows/public-export-check.yml"])
        self.assertFalse((out / ".dev").exists())


class SubmoduleUrlTest(unittest.TestCase):
    def test_private_urls_rewritten_public_kept(self):
        modules = (
            '[submodule "a"]\n\tpath = localmods/synth-priv\n'
            "\turl = https://github.com/Blockfield/synth-priv.git\n"
            '[submodule "b"]\n\tpath = localmods/synth-pub\n'
            "\turl = https://github.com/Blockfield/synth-pub.git\n"
        )
        docs = (
            "see https://github.com/Blockfield/synth-priv/blob/main/docs/X.md\n"
            "and https://github.com/Blockfield/synth-pub/blob/main/docs/Y.md\n"
        )
        public = json.loads(CONFIG.read_text())
        public["public_submodule_paths"] = ["localmods/synth-pub"]
        repo, summary, out = export_fixture(
            self, {".gitmodules": modules, "docs/n.md": docs}, public=public
        )
        text = (out / "docs" / "n.md").read_text()
        self.assertNotIn("synth-priv/blob", text)
        self.assertIn("<private-submodule>", text)
        self.assertIn(
            "https://github.com/Blockfield/synth-pub/blob/main/docs/Y.md", text
        )
        self.assertFalse((out / ".gitmodules").exists())


class CheckModeTest(unittest.TestCase):
    def test_check_passes_clean_tree(self):
        repo, summary, out = export_fixture(self, {"docs/n.md": "clean\n"})
        result = public_export.check_tree(out)
        self.assertEqual(result["tree_files"], summary["tree_files"])

    def test_check_fails_dirty_tree(self):
        repo, summary, out = export_fixture(self, {"docs/n.md": "clean\n"})
        (out / "docs" / "evil.md").write_text(
            "contact qa.synth" + "@acme-example-test.com\n"
        )
        with self.assertRaises(SystemExit):
            public_export.check_tree(out)

    def test_check_ignores_generated_files(self):
        repo, summary, out = export_fixture(self, {"docs/n.md": "clean\n"})
        (out / ".git").mkdir()
        (out / ".git" / "index").write_bytes(b"\x00\x01binary")
        cache = out / ".cache" / "quality"
        cache.mkdir(parents=True)
        (cache / "tool").write_bytes(b"\x00\x01binary")
        pycache = out / "scripts" / "__pycache__"
        pycache.mkdir(parents=True)
        (pycache / "x.pyc").write_bytes(b"\x00\x01binary")
        result = public_export.check_tree(out)
        self.assertEqual(result["tree_files"], summary["tree_files"])

    def test_check_reads_force_tracked_generated_files(self):
        repo = make_repo(self, {"docs/n.md": "clean\n", ".gitignore": ".cache/\n"})
        wf = repo / ".github" / "workflows" / "public-export-check.yml"
        wf.parent.mkdir(parents=True)
        wf.write_text(public_export.render_workflow())
        tracked = repo / ".cache" / "eval.txt"
        tracked.parent.mkdir(parents=True)
        tracked.write_text("config in /ho" + "me/acme-qa/private.txt here\n")
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@localhost",
                "add",
                "-f",
                ".cache/eval.txt",
                ".github/workflows/public-export-check.yml",
            ],
            cwd=repo,
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@localhost",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-qm",
                "tracked",
            ],
            cwd=repo,
            check=True,
        )
        with self.assertRaises(SystemExit) as ctx:
            public_export.check_tree(repo)
        self.assertIn("f-home-dir", str(ctx.exception))

    def test_gitleaks_gets_absolute_out(self):
        repo = make_repo(self, {"n.txt": "clean\n"})
        pub, priv = write_policy_files(
            self,
            json.loads(CONFIG.read_text()),
            {
                "version": 1,
                "replacements": [],
                "drop_sections": [],
                "banned_tokens": [],
            },
        )
        captured = {}
        real_run = subprocess.run

        def fake_run(cmd, **kwargs):
            if isinstance(cmd, list) and len(cmd) > 1 and cmd[1] == "detect":
                captured["source"] = cmd[cmd.index("--source") + 1]
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return real_run(cmd, **kwargs)

        work = Path(tempfile.mkdtemp(prefix="export-cwd-"))
        self.addCleanup(shutil.rmtree, work, True)
        previous = os.getcwd()
        os.chdir(work)
        self.addCleanup(os.chdir, previous)
        with mock.patch.object(public_export.subprocess, "run", side_effect=fake_run):
            with mock.patch.object(
                public_export, "ensure_gitleaks", return_value="/tmp/fake-gitleaks"
            ):
                public_export.main(
                    [
                        "--ref",
                        "HEAD",
                        "--out",
                        "rel-out",
                        "--config",
                        pub,
                        "--private",
                        priv,
                    ],
                    repo=repo,
                )
        self.assertTrue(Path(captured["source"]).is_absolute)

    def test_simulated_public_ci(self):
        policy = real_policy(self)
        files, gitlinks, symlinks, specials, gitmodules = (
            public_export.read_ref_snapshot("HEAD", public_export.ROOT)
        )
        root = HERE.parents[3]
        for name in TOOLING:
            data = (root / "scripts" / name).read_bytes()
            files[f"scripts/{name}"] = (data, 0o644)
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=root,
            check=True,
            stdout=subprocess.PIPE,
        ).stdout.decode("utf-8")
        for chunk in status.split("\0"):
            if len(chunk) < 4:
                continue
            flag, rel = chunk[:2], chunk[3:]
            target = root / rel
            if rel == ".dev" or rel.startswith(".dev/"):
                continue
            if flag.strip() == "?" and not target.is_file():
                continue
            if target.is_file():
                files[rel] = (target.read_bytes(), files.get(rel, (b"", 0o644))[1])
        payload, applied, dropped = public_export.build_payload(
            files, gitlinks, symlinks, specials, gitmodules, policy
        )
        out = Path(tempfile.mkdtemp(prefix="export-sim-"))
        self.addCleanup(shutil.rmtree, out, True)
        public_export.write_output(out, payload)
        self.assertFalse((out / "scripts" / "public_export_private.json").exists())
        init = subprocess.run(["git", "init", "-q"], cwd=out)
        self.assertEqual(init.returncode, 0)
        subprocess.run(["git", "add", "-A"], cwd=out, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@localhost",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-qm",
                "sim",
            ],
            cwd=out,
            check=True,
        )
        # Actual CI order: unit tests first, then the re-check.
        tests = subprocess.run(
            [
                sys.executable,
                "-I",
                "-m",
                "unittest",
                "discover",
                "-s",
                "scripts/dev/tests",
                "-p",
                "test_public_export*.py",
            ],
            cwd=out,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.assertEqual(tests.returncode, 0, tests.stdout[-2000:])
        result = public_export.check_tree(out)
        self.assertTrue(result["tree_files"] > 0)


class GitleaksCacheTest(unittest.TestCase):
    def test_cached_executable_is_reverified(self):
        binary = public_export.ensure_gitleaks()
        good = binary.read_bytes()
        try:
            binary.write_bytes(b"corrupt")
            restored = public_export.ensure_gitleaks()
            self.assertEqual(restored.read_bytes(), good)
        finally:
            binary.write_bytes(good)


@unittest.skipUnless(HAS_PRIVATE, "private export policy absent")
class PrivatePolicyTest(unittest.TestCase):
    def test_pr_snapshot_passes_full_gate(self):
        policy = public_export.load_policy(str(CONFIG), str(PRIVATE))
        files, gitlinks, symlinks, specials, gitmodules = (
            public_export.read_ref_snapshot("HEAD", public_export.ROOT)
        )
        root = HERE.parents[3]
        for name in TOOLING:
            data = (root / "scripts" / name).read_bytes()
            files[f"scripts/{name}"] = (data, 0o644)
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=root,
            check=True,
            stdout=subprocess.PIPE,
        ).stdout.decode("utf-8")
        for chunk in status.split("\0"):
            if len(chunk) < 4:
                continue
            flag, rel = chunk[:2], chunk[3:]
            target = root / rel
            if rel == ".dev" or rel.startswith(".dev/"):
                continue
            if flag.strip() == "?" and not target.is_file():
                continue
            if target.is_file():
                files[rel] = (target.read_bytes(), files.get(rel, (b"", 0o644))[1])
        payload, applied, dropped = public_export.build_payload(
            files, gitlinks, symlinks, specials, gitmodules, policy
        )
        public_export.verify_allowed_binaries(payload, policy["allowed_binaries"])
        workflows = sorted(r for r in payload if r.startswith(".github/workflows/"))
        self.assertEqual(workflows, [policy["workflow_path"]])
        self.assertFalse(any(r == ".dev" or r.startswith(".dev/") for r in payload))
        _, sub_gate = public_export.submodule_rules(
            gitmodules, policy.get("public_submodule_paths", [])
        )
        for rel, entry in payload.items():
            text = (
                entry[1]
                if entry[0] == "link"
                else public_export.decode_text(entry[1])[0]
            )
            if text is not None:
                self.assertEqual(public_export.scan_text(rel, text, sub_gate), [], rel)
        for name in TOOLING:
            self.assertEqual(
                payload[f"scripts/{name}"][1], (root / "scripts" / name).read_bytes()
            )

    def test_banned_token_coverage(self):
        policy = public_export.load_policy(str(CONFIG), str(PRIVATE))
        for entry in policy["banned_tokens"]:
            token = entry["token"] if isinstance(entry, dict) else entry
            hint = entry.get("hint", "?") if isinstance(entry, dict) else "?"
            variants = list(dict.fromkeys([token, token.title()]))
            for variant in variants:
                with self.subTest(token=hint):
                    files = {"probe.md": (f"prefix {variant} suffix\n".encode(), 0o644)}
                    try:
                        payload, applied, _ = public_export.build_payload(
                            files, [], [], [], None, policy
                        )
                    except SystemExit:
                        continue  # fail-closed gate caught it: covered
                    changed = any(
                        e[1] != files[r][0]
                        for r, e in payload.items()
                        if e[0] == "file" and r in files
                    )
                    self.assertTrue(changed or applied, variant)

    def test_mixed_case_domain_gate(self):
        policy = public_export.load_policy(str(CONFIG), str(PRIVATE))
        entry = next(
            e
            for e in policy["banned_tokens"]
            if isinstance(e, dict) and e.get("hint") == "domain"
        )
        variant = entry["token"].title()
        self.assertNotEqual(variant, entry["token"])
        self.assertEqual(variant.casefold(), entry["token"].casefold())
        cases = {
            "probe.md": f"Service URL: https://{variant}/docs\n",
            f"docs/{variant}.md": "clean\n",
        }
        for rel, content in cases.items():
            with self.subTest(path=rel):
                files = {rel: (content.encode(), 0o644)}
                with self.assertRaises(SystemExit):
                    public_export.build_payload(files, [], [], [], None, policy)

    def test_mixed_case_identity_third_party_gate(self):
        # Every identity/third-party name is identifying in any
        # capitalization: a mixed-case variant in an ordinary document or
        # filename must be rejected. Values load from the excluded private
        # policy at runtime only, never spelled in this file.
        policy = public_export.load_policy(str(CONFIG), str(PRIVATE))
        entries = [
            e
            for e in policy["banned_tokens"]
            if isinstance(e, dict) and e.get("hint") in ("identity", "third-party")
        ]
        self.assertTrue(entries)
        for entry in entries:
            token = entry["token"]
            variant = token.title()
            if variant in (token, token.upper()):
                variant = "".join(
                    c.upper() if i % 2 else c.lower() for i, c in enumerate(token)
                )
            self.assertEqual(variant.casefold(), token.casefold())
            cases = {
                "probe.md": f"Private owner/service: {variant}\n",
                f"docs/{variant}.md": "clean\n",
            }
            for rel, content in cases.items():
                with self.subTest(hint=entry.get("hint"), path=rel):
                    files = {rel: (content.encode(), 0o644)}
                    with self.assertRaises(SystemExit):
                        public_export.build_payload(files, [], [], [], None, policy)

    def test_date_stamped_backup_filename_rejected(self):
        # Short infrastructure node names stay identifying when glued to a
        # date stamp: substring matching applies in contents and filenames.
        # Values load from the excluded private policy at runtime only.
        policy = real_policy(self)
        node = next(
            entry["token"]
            for entry in policy["banned_tokens"]
            if isinstance(entry, dict)
            and entry.get("hint") == "node"
            and len(entry["token"]) == 7
        )
        cases = [
            {"probe.md": f"Backup file: {node}.log\n"},
            {"probe.md": f"Backup file: {node}20261029.log\n"},
            {f"docs/{node}20261029.log": "clean\n"},
        ]
        for files in cases:
            with self.subTest(files=sorted(files)):
                encoded = {
                    rel: (data.encode("utf-8"), 0o644) for rel, data in files.items()
                }
                with self.assertRaises(SystemExit):
                    public_export.build_payload(encoded, [], [], [], None, policy)

    def test_shipped_files_contain_no_private_literals(self):
        policy = public_export.load_policy(str(CONFIG), str(PRIVATE))
        variants = set()
        for entry in policy["banned_tokens"]:
            token = entry["token"] if isinstance(entry, dict) else entry
            variants.add(token)
            variants.add(token.upper())
        texts = {
            p.relative_to(HERE.parents[3]).as_posix(): p.read_text(encoding="utf-8")
            for p in SHIPPED
        }
        for rel, text in texts.items():
            hits = sum(1 for v in variants if v and v in text)
            self.assertFalse(hits, f"private literals in shipped file: {rel}")
            findings = public_export.scan_text(
                rel, text, public_export.load_config(str(CONFIG))["forbidden"]
            )
            self.assertEqual(findings, [], rel)

    def test_folded_fragments_contain_no_private_literals(self):
        policy = public_export.load_policy(str(CONFIG), str(PRIVATE))
        variants = set()
        for entry in policy["banned_tokens"]:
            token = entry["token"] if isinstance(entry, dict) else entry
            variants.add(token)
            variants.add(token.upper())
        for path in SHIPPED:
            if path.suffix != ".py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            joined = _folded_strings(tree)
            rel = path.relative_to(HERE.parents[3]).as_posix()
            hits = [v for v in variants if v and any(v in s for s in joined)]
            self.assertEqual(hits, [], rel)

    def test_folded_fragments_catch_split_token(self):
        policy = public_export.load_policy(str(CONFIG), str(PRIVATE))
        tokens = [
            entry["token"] if isinstance(entry, dict) else entry
            for entry in policy["banned_tokens"]
        ]
        tokens = [t for t in tokens if len(t) >= 8]
        self.assertTrue(tokens, "need a multi-char token for the fold probe")
        token = sorted(tokens, key=len)[-1]
        left, right = token[: len(token) // 2], token[len(token) // 2 :]
        source = "x = " + json.dumps(left) + " + " + json.dumps(right) + "\n"
        folded = _folded_strings(ast.parse(source))
        self.assertTrue(
            any(token in s for s in folded),
            "split private token must survive constant folding",
        )
        synthetic = _folded_strings(
            ast.parse('x = "acme-" + "widget-42"\ny = "qa" + "@" + "example.test"\n')
        )
        self.assertTrue(synthetic, "synthetic fixture must fold")
        self.assertFalse(
            any(token in s for s in synthetic for token in tokens),
            "synthetic fixture must not match the private inventory",
        )

    def test_missing_private_policy_fails_closed(self):
        repo = make_repo(self, {"n.txt": "clean\n"})
        out = Path(tempfile.mkdtemp(prefix="export-out-"))
        self.addCleanup(shutil.rmtree, out, True)
        with self.assertRaises(SystemExit) as ctx:
            public_export.export_tree(
                "HEAD", out, str(CONFIG), str(out / "nope.json"), repo=repo
            )
        self.assertIn("private export policy missing", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
