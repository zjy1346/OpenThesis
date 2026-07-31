from __future__ import annotations

import unittest

from openthesis.app import (
    clamp_report_zoom,
    ease_out_cubic,
    format_elapsed,
    friendly_research_error,
)


class ResearchFeedbackTests(unittest.TestCase):
    def test_elapsed_time_format(self) -> None:
        self.assertEqual(format_elapsed(0), "00:00")
        self.assertEqual(format_elapsed(65), "01:05")
        self.assertEqual(format_elapsed(3661), "01:01:01")

    def test_report_animation_helpers_are_bounded(self) -> None:
        self.assertEqual(ease_out_cubic(-1), 0)
        self.assertEqual(ease_out_cubic(0), 0)
        self.assertEqual(ease_out_cubic(1), 1)
        self.assertGreater(ease_out_cubic(0.5), 0.5)
        self.assertEqual(clamp_report_zoom(0.2), 0.8)
        self.assertEqual(clamp_report_zoom(2.0), 1.6)
        self.assertEqual(clamp_report_zoom(1.234), 1.23)

    def test_authentication_error_is_actionable(self) -> None:
        title, guidance = friendly_research_error(
            '模型接口返回 HTTP 401：{"error":"unauthorized"}'
        )
        self.assertEqual(title, "模型认证失败")
        self.assertIn("API Key", guidance)

    def test_timeout_and_rate_limit_are_distinct(self) -> None:
        timeout_title, _ = friendly_research_error("request timed out")
        rate_title, _ = friendly_research_error("HTTP 429 rate limit")
        self.assertIn("超时", timeout_title)
        self.assertIn("限流", rate_title)

    def test_unknown_error_does_not_expose_false_diagnosis(self) -> None:
        title, guidance = friendly_research_error("unexpected response")
        self.assertEqual(title, "研究任务失败")
        self.assertIn("中间结果", guidance)

    def test_sec_error_is_not_misclassified_as_model_404(self) -> None:
        title, guidance = friendly_research_error("SEC 请求失败（HTTP 404）")
        self.assertEqual(title, "SEC 数据获取失败")
        self.assertIn("SEC 联系邮箱", guidance)

    def test_error_guidance_supports_english_interface(self) -> None:
        title, guidance = friendly_research_error(
            "model endpoint returned HTTP 401", "en"
        )
        self.assertEqual(title, "Model Authentication Failed")
        self.assertIn("API key", guidance)


if __name__ == "__main__":
    unittest.main()
