import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class TelegramRoutingTests(unittest.TestCase):
    def test_every_telegram_workflow_passes_chat_and_topic(self):
        workflows = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
        checked = 0
        for path in workflows:
            text = path.read_text(encoding="utf-8")
            if "TELEGRAM_BOT_TOKEN" not in text:
                continue
            checked += 1
            with self.subTest(workflow=path.name):
                self.assertIn("TELEGRAM_CHAT_ID", text)
                self.assertIn("TELEGRAM_MESSAGE_THREAD_ID", text)
                self.assertIn("TELEGRAM_SECONDARY_CHAT_ID", text)
        self.assertGreater(checked, 0)

    def test_only_shared_helper_calls_telegram_send_message_api(self):
        offenders = []
        for path in ROOT.glob("*.py"):
            if path.name == "bot_common.py":
                continue
            if "/sendMessage" in path.read_text(encoding="utf-8"):
                offenders.append(path.name)
        self.assertEqual(offenders, [])

    def test_telegram_templates_do_not_expose_internal_owner(self):
        offenders = [
            path.name
            for path in ROOT.glob("*.py")
            if "Owner:" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(offenders, [])


class RepositoryReferenceTests(unittest.TestCase):
    def test_workflow_python_entrypoints_exist(self):
        pattern = re.compile(r"^\s*run:\s*python\s+([^\s]+\.py)\s*$", re.MULTILINE)
        for path in (ROOT / ".github" / "workflows").glob("*.yml"):
            text = path.read_text(encoding="utf-8")
            for relative in pattern.findall(text):
                with self.subTest(workflow=path.name, entrypoint=relative):
                    self.assertTrue((ROOT / relative).is_file())

    def test_all_config_and_state_json_is_valid(self):
        paths = sorted((ROOT / "config").glob("*.json"))
        paths += sorted((ROOT / "state").glob("*.json"))
        self.assertGreater(len(paths), 0)
        for path in paths:
            with self.subTest(path=str(path.relative_to(ROOT))):
                json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
