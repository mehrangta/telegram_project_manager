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

    def test_primary_id_commands_get_native_copy_buttons(self):
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
                "/edit i-12345678",
                "/confirm i-12345678",
                "/cancel i-12345678",
            ],
        )

    def test_empty_keyboard_can_be_serialized_to_remove_stale_buttons(self):
        outgoing = outgoing_message("Status")
        self.assertIsNone(outgoing.reply_markup())
        self.assertEqual(outgoing.reply_markup(include_empty=True), {"inline_keyboard": []})

    def test_copy_button_rejects_telegram_limit_violation(self):
        with self.assertRaises(ValueError):
            InlineButton("Copy", copy_text="x" * 257)


if __name__ == "__main__":
    unittest.main()
