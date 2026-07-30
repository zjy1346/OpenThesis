from __future__ import annotations

import re

from . import __version__
from .domain import Company
from .i18n import sec_profile_id_from_label


SEC_DEFAULT_PROFILE = "personal"
SEC_PROFILE_AGENTS = {
    "personal": "Personal Investor",
    "independent": "Independent Researcher",
    "organization": "Organization Research Team",
}
SEC_PROFILE_LABELS = tuple(SEC_PROFILE_AGENTS)

_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_EMAIL_SEARCH_PATTERN = re.compile(r"[^@\s()<>;,]+@[^@\s()<>;,]+\.[^@\s()<>;,]+")


def validate_sec_contact_email(email: str) -> str:
    normalized = email.strip()
    if not _EMAIL_PATTERN.fullmatch(normalized):
        raise ValueError("请输入你本人或所在研究团队可正常收信的邮箱地址。")
    return normalized


def build_sec_user_agent(profile: str, email: str) -> str:
    profile = sec_profile_id_from_label(profile)
    if profile not in SEC_PROFILE_AGENTS:
        raise ValueError("请选择一个 SEC 请求身份模板。")
    normalized_email = validate_sec_contact_email(email)
    identity = SEC_PROFILE_AGENTS[profile]
    return f"OpenThesis/{__version__} ({identity}; contact: {normalized_email})"


def extract_sec_contact_email(user_agent: str) -> str:
    match = _EMAIL_SEARCH_PATTERN.search(user_agent)
    return match.group(0) if match else ""


COMMON_COMPANIES = (
    Company(cik="0000320193", ticker="AAPL", name="Apple Inc.", exchange="Nasdaq"),
    Company(
        cik="0000789019",
        ticker="MSFT",
        name="Microsoft Corporation",
        exchange="Nasdaq",
    ),
    Company(
        cik="0001652044",
        ticker="GOOGL",
        name="Alphabet Inc.",
        exchange="Nasdaq",
    ),
    Company(
        cik="0001018724",
        ticker="AMZN",
        name="Amazon.com, Inc.",
        exchange="Nasdaq",
    ),
    Company(
        cik="0001045810",
        ticker="NVDA",
        name="NVIDIA Corporation",
        exchange="Nasdaq",
    ),
    Company(
        cik="0001326801",
        ticker="META",
        name="Meta Platforms, Inc.",
        exchange="Nasdaq",
    ),
    Company(cik="0001318605", ticker="TSLA", name="Tesla, Inc.", exchange="Nasdaq"),
    Company(
        cik="0001067983",
        ticker="BRK.B",
        name="Berkshire Hathaway Inc.",
        exchange="NYSE",
    ),
    Company(
        cik="0000019617",
        ticker="JPM",
        name="JPMorgan Chase & Co.",
        exchange="NYSE",
    ),
    Company(
        cik="0000021344",
        ticker="KO",
        name="The Coca-Cola Company",
        exchange="NYSE",
    ),
)


def common_company_label(company: Company) -> str:
    return f"{company.ticker} · {company.name}"


COMMON_COMPANY_BY_LABEL = {
    common_company_label(company): company for company in COMMON_COMPANIES
}
COMMON_COMPANY_LABELS = tuple(COMMON_COMPANY_BY_LABEL)


def get_common_company(label: str) -> Company:
    try:
        company = COMMON_COMPANY_BY_LABEL[label]
    except KeyError as exc:
        raise ValueError("请选择一个内置的常用公司。") from exc
    return Company(**company.to_dict())
