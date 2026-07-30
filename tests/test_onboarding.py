import unittest

from openthesis.onboarding import (
    COMMON_COMPANIES,
    SEC_DEFAULT_PROFILE,
    SEC_PROFILE_LABELS,
    build_sec_user_agent,
    extract_sec_contact_email,
    get_common_company,
    common_company_label,
    validate_sec_contact_email,
)


class SecOnboardingTests(unittest.TestCase):
    def test_profiles_build_identifiable_user_agents(self) -> None:
        for profile in SEC_PROFILE_LABELS:
            with self.subTest(profile=profile):
                value = build_sec_user_agent(profile, "investor@example.com")
                self.assertIn("OpenThesis/", value)
                self.assertIn("investor@example.com", value)

    def test_invalid_contact_email_is_rejected(self) -> None:
        for value in ("", "not-an-email", "name@example", "two words@example.com"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_sec_contact_email(value)

    def test_legacy_user_agent_email_can_be_migrated(self) -> None:
        self.assertEqual(
            extract_sec_contact_email("OpenThesis legacy@example.com"),
            "legacy@example.com",
        )
        self.assertEqual(extract_sec_contact_email("OpenThesis"), "")

    def test_default_profile_is_available(self) -> None:
        self.assertIn(SEC_DEFAULT_PROFILE, SEC_PROFILE_LABELS)


class CommonCompanyPresetTests(unittest.TestCase):
    def test_company_presets_have_unique_tickers_and_sec_ciks(self) -> None:
        tickers = {company.ticker for company in COMMON_COMPANIES}
        self.assertEqual(len(tickers), len(COMMON_COMPANIES))
        for company in COMMON_COMPANIES:
            self.assertRegex(company.cik, r"^\d{10}$")

    def test_company_lookup_returns_a_copy(self) -> None:
        source = COMMON_COMPANIES[0]
        selected = get_common_company(common_company_label(source))
        self.assertEqual(selected.to_dict(), source.to_dict())
        self.assertIsNot(selected, source)


if __name__ == "__main__":
    unittest.main()
