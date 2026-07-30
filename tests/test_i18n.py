from __future__ import annotations

import unittest

from openthesis.i18n import (
    EN,
    LANGUAGE_NAMES,
    MODEL_PRESET_LABELS,
    SEC_PROFILE_IDS,
    UI_EN,
    ZH_CN,
    model_preset_id_from_label,
    normalize_language,
    placeholder_names,
    run_status_label,
    sec_profile_id_from_label,
    sec_profile_label,
    translate,
    translate_error,
)
from openthesis.model_catalog import MODEL_PRESETS, get_model_preset, preset_labels


class InternationalizationTests(unittest.TestCase):
    def test_language_normalization_and_fallback(self) -> None:
        self.assertEqual(normalize_language("en-US"), EN)
        self.assertEqual(normalize_language("en_GB"), EN)
        self.assertEqual(normalize_language("unknown"), ZH_CN)
        self.assertEqual(translate("设置", "unknown"), "设置")
        self.assertEqual(LANGUAGE_NAMES[EN], "English")

    def test_english_templates_preserve_placeholders(self) -> None:
        for source, english in UI_EN.items():
            self.assertTrue(english.strip(), source)
            self.assertEqual(
                placeholder_names(source),
                placeholder_names(english),
                source,
            )

    def test_model_preset_labels_are_unique_and_round_trip(self) -> None:
        expected_ids = {preset.preset_id for preset in MODEL_PRESETS}
        for language in (ZH_CN, EN):
            labels = preset_labels(language)
            self.assertEqual(len(labels), len(set(labels)))
            self.assertEqual(
                {get_model_preset(label).preset_id for label in labels},
                expected_ids,
            )
        for language_labels in MODEL_PRESET_LABELS.values():
            for preset_id, label in language_labels.items():
                self.assertEqual(model_preset_id_from_label(label), preset_id)

    def test_sec_profiles_use_stable_ids_and_accept_legacy_labels(self) -> None:
        for profile_id in SEC_PROFILE_IDS:
            for language in (ZH_CN, EN):
                label = sec_profile_label(profile_id, language)
                self.assertEqual(sec_profile_id_from_label(label), profile_id)
        self.assertEqual(sec_profile_id_from_label("个人投资者（推荐）"), "personal")

    def test_error_and_status_translation(self) -> None:
        self.assertEqual(run_status_label("partial", EN), "Partially Complete")
        self.assertIn(
            "Connection successful",
            translate_error("连接成功", EN),
        )
        self.assertIn(
            "unsafe path",
            translate_error("研究包包含不安全路径：../bad", EN),
        )


if __name__ == "__main__":
    unittest.main()
