import unittest

from telegram_project_manager.platform.responses import (
    InlineButton,
    outgoing_message,
)


class ResponsePresentationTests(unittest.TestCase):
    def test_escapes_dynamic_content_and_collapses_plan(self):
        outgoing = outgoing_message(
            "⏸️ Codex code job\n"
            "Code Job ID: c-abcdef12\n"
            "Issue: owner/repo#1 — <unsafe & title>\n\n"
            "Plan revision 1: Keep <tags> literal\n"
            "1. Update parser\n"
            "CI checks: ✅ 2  ⏳ 0  ❌ 0",
            expandable_prefixes=("Plan revision",),
        )

        self.assertIn("⏸️ <b>Codex code job</b>", outgoing.text)
        self.assertIn("<code>c-abcdef12</code>", outgoing.text)
        self.assertIn("&lt;unsafe &amp; title&gt;", outgoing.text)
        self.assertIn("<blockquote expandable>", outgoing.text)
        self.assertNotIn("<tags>", outgoing.text)

    def test_primary_id_commands_get_native_action_buttons(self):
        outgoing = outgoing_message(
            "Issue draft created.\n"
            "Draft ID: i-12345678\n"
            "/edit i-12345678 <feedback>\n"
            "/confirm i-12345678\n"
            "/cancel i-12345678"
        )
        copied = [
            button.copy_text
            for row in outgoing.keyboard
            for button in row
            if button.copy_text
        ]

        self.assertEqual(
            copied,
            [
                "i-12345678",
            ],
        )
        callbacks = [
            button.callback_data
            for row in outgoing.keyboard
            for button in row
            if button.callback_data
        ]
        self.assertEqual(
            callbacks,
            [
                "edit_issue:i-12345678",
                "command:/confirm i-12345678",
                "command:/cancel i-12345678",
            ],
        )

    def test_non_issue_placeholder_command_remains_copy_action(self):
        outgoing = outgoing_message(
            "Codex code job\n"
            "Code Job ID: c-abcdef12\n"
            "Edit: /code edit c-abcdef12 <feedback>"
        )
        copied = [
            button.copy_text
            for row in outgoing.keyboard
            for button in row
            if button.copy_text
        ]
        callbacks = [
            button.callback_data
            for row in outgoing.keyboard
            for button in row
            if button.callback_data
        ]

        self.assertEqual(copied, ["c-abcdef12", "/code edit c-abcdef12"])
        self.assertEqual(callbacks, [])

    def test_merge_and_deploy_require_confirmation_while_rebase_runs_directly(self):
        outgoing = outgoing_message(
            "Codex code job\n"
            "Code Job ID: c-abcdef12\n"
            "Rebase onto latest base: /code rebase c-abcdef12\n"
            "Merge: /merge c-abcdef12\n"
            "Deploy: /deploy c-abcdef12"
        )
        callbacks = [
            button.callback_data
            for row in outgoing.keyboard
            for button in row
            if button.callback_data
        ]

        self.assertEqual(
            callbacks,
            [
                "command:/code rebase c-abcdef12",
                "confirm_merge:c-abcdef12",
                "confirm_deploy:c-abcdef12",
            ],
        )

    def test_empty_keyboard_can_be_serialized_to_remove_stale_buttons(self):
        outgoing = outgoing_message("Status")
        self.assertIsNone(outgoing.reply_markup())
        self.assertEqual(outgoing.reply_markup(include_empty=True), {"inline_keyboard": []})

    def test_do_job_id_gets_copy_and_status_controls(self):
        outgoing = outgoing_message(
            "⚙️ Codex do job\n"
            "Do Job ID: d-12345678\n"
            "Status command: /do status d-12345678"
        )
        copied = [button.copy_text for row in outgoing.keyboard for button in row if button.copy_text]
        callbacks = [
            button.callback_data for row in outgoing.keyboard for button in row if button.callback_data
        ]
        self.assertEqual(copied, ["d-12345678"])
        self.assertEqual(callbacks, ["command:/do status d-12345678"])

    def test_copy_button_rejects_telegram_limit_violation(self):
        with self.assertRaises(ValueError):
            InlineButton("Copy", copy_text="x" * 257)

    def test_callback_button_rejects_telegram_byte_limit_violation(self):
        with self.assertRaises(ValueError):
            InlineButton("Action", callback_data="é" * 33)


if __name__ == "__main__":
    unittest.main()
