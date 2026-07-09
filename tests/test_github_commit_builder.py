import json
import unittest

from telegram_project_manager.integrations.gh.runner import GhResult


class GhResultTests(unittest.TestCase):
    def test_json_output(self):
        result = GhResult(args=["gh"], returncode=0, stdout=json.dumps({"ok": True}), stderr="", duration_ms=1)
        self.assertEqual(result.json(), {"ok": True})


if __name__ == "__main__":
    unittest.main()

