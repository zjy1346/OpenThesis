from copy import deepcopy
import pytest

from openthesis.company_search import OfficialCompanySearch, SEARCH_CONTRACT
from openthesis.market_data import CnInfoAdapter, HkexNewsAdapter, MarketDataError
from openthesis.markets import build_company
from openthesis.official_network import OfficialNetworkError


class Settings:
    def __init__(self):
        self.values = {}
    def get_setting(self, key, default=""):
        return self.values.get(key, default)
    def set_setting(self, key, value):
        self.values[key] = value


def unavailable():
    raise OfficialNetworkError("blocked", code="SOURCE_DNS_POLICY_BLOCKED")


@pytest.mark.parametrize("query", ("00992.HK", "00992.hk", "00992", "992"))
def test_hk_security_identifier_is_normalized_before_name_punctuation(query):
    adapter = HkexNewsAdapter()
    adapter._stock_rows = [{
        "code": "00992", "name": "聯想集團", "stock_id": "official-00992",
        "search_text": "联想集团 lenovo",
    }]
    assert [item.ticker for item in adapter.resolve(query)] == ["00992.HK"]


def test_failure_does_not_synthesize_a_verified_issuer():
    search = OfficialCompanySearch(Settings())
    result = search.search("000811", "CN_A", unavailable, request_id="seq-1")
    assert result.state == "unavailable"
    assert result.companies[0]["identity_state"] == "unresolved_code"
    assert result.diagnostics[0]["code"] == "SOURCE_DNS_POLICY_BLOCKED"
    assert result.to_dict()["contract"] == SEARCH_CONTRACT
    with pytest.raises(OfficialNetworkError):
        search.require_identity(build_company("000811", "000811", market="CN_A"), unavailable)


def test_official_zero_is_empty_not_network_failure_or_cached_success():
    store = Settings()
    search = OfficialCompanySearch(store)
    company = build_company("000811", "冰轮环境", market="CN_A", issuer_id="CNINFO:811")
    search.search("冰轮", "CN_A", lambda: [company])
    assert search.search("冰轮", "CN_A", lambda: []).state == "empty"
    assert search.search("冰轮", "CN_A", lambda: []).companies == ()


def test_directory_survives_new_search_instance_and_preserves_provenance():
    store = Settings()
    company = build_company("000811", "冰轮环境", market="CN_A", issuer_id="CNINFO:811")
    before = deepcopy(company.to_dict())
    fresh = OfficialCompanySearch(store).search("冰轮", "CN_A", lambda: [company])
    restored = OfficialCompanySearch(store).search("000811", "CN_A", unavailable)
    assert restored.state == "degraded"
    assert restored.companies[0]["name"] == "冰轮环境"
    assert restored.companies[0]["identity_state"] == "cached_verified"
    assert restored.companies[0]["identity_verified_at"] == fresh.companies[0]["identity_verified_at"]
    assert OfficialCompanySearch(store).require_identity(company, unavailable).to_dict() == before
    assert company.to_dict() == before


def test_wrong_market_or_code_as_name_cannot_seed_verified_cache():
    store = Settings()
    search = OfficialCompanySearch(store)
    for company in (build_company("000811", "000811", market="CN_A"), build_company("700", "Tencent", market="HK")):
        result = search.search("000811", "CN_A", lambda: [company])
        assert result.state == "unavailable"
        assert result.diagnostics[0]["code"] == "SOURCE_IDENTITY_INVALID"
    assert store.values == {}


def test_cn_adapter_no_longer_swallows_failure_into_static_fallback():
    class Transport:
        def post_form(self, *args):
            raise MarketDataError("DNS blocked", code="SOURCE_DNS_POLICY_BLOCKED")
    with pytest.raises(MarketDataError):
        CnInfoAdapter(Transport()).resolve("000811")


def test_cn_code_suffix_uses_official_code_query():
    class Transport:
        def post_form(self, url, fields):
            assert fields["keyWord"] == "000811"
            return [{"code": "000811", "zwjc": "冰轮环境", "orgId": "811"}]
    assert CnInfoAdapter(Transport()).resolve("000811.SZ")[0].name == "冰轮环境"
