from __future__ import annotations

import unittest

from openthesis.app import OpenThesisApp


class _Variable:
    def __init__(self, value: str = ""):
        self.value = value

    def get(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        self.value = value


class _Storage:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def set_setting(self, key: str, value: str) -> None:
        self.values[key] = value


class LanguageSettingsBehaviorTests(unittest.TestCase):
    def test_ui_language_waits_for_restart_and_report_language_is_immediate(self) -> None:
        app = OpenThesisApp.__new__(OpenThesisApp)
        app.ui_language = "zh-CN"
        app.report_language = "zh-CN"
        app.ui_language_var = _Variable("English")
        app.report_language_var = _Variable("English")
        app.language_settings_status_var = _Variable()
        app.status_var = _Variable()
        app.storage = _Storage()
        app.current_run_id = ""

        app._save_language_settings()

        self.assertEqual(app.ui_language, "zh-CN")
        self.assertEqual(app.report_language, "en")
        self.assertEqual(app.storage.values["ui_language"], "en")
        self.assertEqual(app.storage.values["report_language"], "en")
        self.assertNotIn("api_key", app.storage.values)
        self.assertIn("重启", app.language_settings_status_var.value)


if __name__ == "__main__":
    unittest.main()
