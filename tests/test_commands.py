import unittest

from telegram_project_manager.bots.commit_manager.commands import split_command


class CommandTests(unittest.TestCase):
    def test_split_command(self):
        self.assertEqual(split_command("/commit add readme"), ("/commit", "add readme"))

    def test_split_command_with_bot_username(self):
        self.assertEqual(split_command("/commit@MyBot add readme"), ("/commit", "add readme"))


if __name__ == "__main__":
    unittest.main()

