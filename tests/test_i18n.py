from __future__ import annotations

import unittest
import json
from pathlib import Path

from openthesis.i18n import (
    EN,
    ZH_HANT,
    LANGUAGE_NAMES,
    MODEL_PRESET_LABELS,
    OUTPUT_LANGUAGE_INSTRUCTIONS,
    SEC_PROFILE_IDS,
    UI_EN,
    UI_HANT,
    LANGUAGE_REGISTRY,
    SUPPORTED_LANGUAGES,
    ZH_CN,
    model_preset_id_from_label,
    normalize_language,
    language_name,
    resolve_system_language,
    resolve_ui_language,
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
        self.assertEqual(normalize_language("zh-TW"), ZH_HANT)
        self.assertEqual(normalize_language("zh_Hant_HK"), ZH_HANT)
        self.assertEqual(normalize_language("zh-Hans-SG"), ZH_CN)
        self.assertEqual(resolve_system_language(("fr-FR", "zh-HK")), ZH_HANT)
        self.assertEqual(resolve_system_language(("fr-FR",)), EN)
        self.assertEqual(resolve_ui_language("manual", "en", ("zh-TW",)), EN)
        self.assertEqual(resolve_ui_language("system", "en", ("zh-TW",)), ZH_HANT)
        self.assertEqual(resolve_ui_language("unknown", "legacy", ()), ZH_CN)

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
        for language in (ZH_CN, ZH_HANT, EN):
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
            for language in (ZH_CN, ZH_HANT, EN):
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
        translated_catalog_error = translate_error(
            "认证失败（HTTP 401，地址 api.moonshot.ai），请检查 API Key、区域预设和账号权限。",
            EN,
        )
        self.assertIn("Authentication failed (HTTP 401, endpoint api.moonshot.ai)", translated_catalog_error)
        self.assertNotIn("区域预设", translated_catalog_error)

    def test_hant_catalog_is_explicit_and_output_instruction_is_stable(self) -> None:
        self.assertEqual(language_name(ZH_HANT), "繁體中文")
        self.assertIn("\u8a2d\u5b9a", translate("\u8bbe\u7f6e", ZH_HANT))
        self.assertIn("Traditional Chinese", OUTPUT_LANGUAGE_INSTRUCTIONS[ZH_HANT])

    def test_shared_language_contract_matches_python_registry(self) -> None:
        contract = json.loads((Path(__file__).parents[1] / "language-contract.json").read_text(encoding="utf-8"))
        self.assertEqual(contract["fallback"], "en")
        self.assertEqual(contract["unknown_legacy_fallback"], ZH_CN)
        self.assertEqual([item["id"] for item in contract["languages"]], list(SUPPORTED_LANGUAGES))
        for item, definition in zip(contract["languages"], LANGUAGE_REGISTRY):
            self.assertEqual(item["id"], definition.canonical)
            self.assertEqual(tuple(item["aliases"]), definition.aliases)
            self.assertEqual(tuple(item["localePrefixes"]), definition.locale_prefixes)
            self.assertEqual(item["htmlLang"], definition.html_lang)
            self.assertEqual(item["dir"], definition.direction)
            self.assertEqual(item["names"][ZH_CN], language_name(definition.canonical, ZH_CN))
            self.assertEqual(item["names"][ZH_HANT], language_name(definition.canonical, ZH_HANT))
            self.assertEqual(item["names"][EN], language_name(definition.canonical, EN))

    def test_hant_catalog_has_complete_keys_and_no_common_simplified_glyphs(self) -> None:
        self.assertTrue(set(UI_EN).issubset(UI_HANT))
        self.assertEqual(translate("跟随系统", ZH_HANT), "跟隨系統")
        self.assertEqual(translate("手动选择", ZH_HANT), "手動選擇")
        self.assertEqual(translate("界面语言模式", ZH_HANT), "介面語言模式")
        simplified_only = set("设报财务语络进发证隐边际现问题产开关请许单从对为长结间问题与还将当让").difference({"发"})
        leaked = {char for value in UI_HANT.values() for char in value if char in simplified_only}
        self.assertEqual(leaked, set())


if __name__ == "__main__":
    unittest.main()
