import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import changed_line_secret_scan


class ChangedLineSecretScanTests(unittest.TestCase):
    def test_scan_flags_nested_ssh_paths_and_private_host_labels(self):
        diff = "\n".join(
            [
                "diff --git a/docs/a.md b/docs/a.md",
                "+++ b/docs/a.md",
                "+ssh key at /Users/example/" + ".ssh/aurum_key",
                "+markdown key `/Users/example/" + ".ssh/aurum_backtick`",
                "+parenthesized key (~/" + ".ssh/aurum_parenthesized)",
                "+host " + "aurum-" + "tokyo-01" + " is reachable",
            ]
        )

        findings = changed_line_secret_scan.scan_diff(diff)

        names = [name for _path, name, _excerpt in findings]
        self.assertGreaterEqual(names.count("ssh_path"), 3)
        self.assertIn("private_host_label", names)

    def test_scan_redacts_secret_like_path_components_in_findings(self):
        private_label = "aurum-" + "testhost-01"
        diff = "\n".join(
            [
                f"diff --git a/docs/{private_label}.md b/docs/{private_label}.md",
                f"+++ b/docs/{private_label}.md",
                "+token = \"abcdefghijklmnop\"",
            ]
        )

        findings = changed_line_secret_scan.scan_diff(diff)

        self.assertTrue(findings)
        self.assertNotIn(private_label, findings[0][0])
        self.assertIn("[redacted]", findings[0][0])

    def test_extra_file_scan_redacts_secret_like_path_components(self):
        private_label = "aurum-" + "testhost-01"
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / f"{private_label}.txt"
            path.write_text('token = "' + "abcdefghijklmnop" + '"\n', encoding="utf-8")

            findings = changed_line_secret_scan.scan_extra_files([str(path)])

        self.assertTrue(findings)
        self.assertNotIn(private_label, findings[0][0])
        self.assertIn("[redacted]", findings[0][0])


if __name__ == "__main__":
    unittest.main()
