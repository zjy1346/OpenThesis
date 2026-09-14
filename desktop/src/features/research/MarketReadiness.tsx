import type { Company, Language, MarketSnapshotPreview } from '../../types';

export function MarketReadiness({ company, language, onSwitch }: {
  company: Company; language: Language; onSwitch: (company: Company) => void;
}) {
  const status = company.research_readiness;
  if (!status || status.recommended) return null;
  const en = language === 'en';
  const hant = language === 'zh-Hant';
  return <div className="provider-hint" role="status">
    <p>{en ? `A continuous five-year disclosure history has not been verified for this listing (${status.consecutive_years} years verified).`
      : hant ? `此上市市場尚未驗證連續五年披露（已驗證 ${status.consecutive_years} 年）。`
      : `此上市市场尚未验证连续五年披露（已验证 ${status.consecutive_years} 年）。`}</p>
    {status.alternative && <button type="button" className="refresh-button" onClick={() => onSwitch(status.alternative!)}>
      {en ? 'Research the same issuer in its A-share market' : hant ? '切換至同一發行人的 A 股市場研究' : '切换至同一发行人的 A 股市场研究'} · {status.alternative.ticker}
    </button>}
  </div>;
}

export function FxDetails({ snapshot, language }: { snapshot: MarketSnapshotPreview | null; language: Language }) {
  if (!snapshot) return null;
  const en = language === 'en';
  const hant = language === 'zh-Hant';
  const reasons: Record<string, string> = {
    FX_UNAVAILABLE: en ? 'A dated exchange rate is unavailable. Retry when the FX source is reachable.' : hant ? '缺少有效日期的匯率，請在匯率來源恢復後重試。' : '缺少有效日期的汇率，请在汇率来源恢复后重试。',
    MARKET_CAP_SCOPE_INCOMPLETE: en ? 'The quote does not establish the total issuer equity value across share classes; valuation is unavailable.' : hant ? '行情未提供覆蓋全部股份類別的發行人總市值，暫不能用於估值。' : '行情未提供覆盖全部股份类别的发行人总市值，暂不能用于估值。',
    QUOTE_CURRENCY_MISMATCH: en ? 'Quote currency conflicts with the listing currency.' : hant ? '行情幣種與上市計價幣種衝突。' : '行情币种与上市计价币种冲突。',
    QUOTE_UNAVAILABLE: en ? 'The quote source is unavailable. Retry later.' : hant ? '行情來源暫不可用，請稍後重試。' : '行情来源暂不可用，请稍后重试。',
    QUOTE_TIMEOUT: en ? 'The quote source timed out.' : hant ? '行情來源請求逾時。' : '行情来源请求超时。',
  };
  const reason = snapshot.error_code || snapshot.warnings?.find(key => reasons[key]);
  if (snapshot.fx_rate && snapshot.fx_as_of && snapshot.fx_source) {
    return <p className="provider-hint">{en ? 'Reference conversion' : hant ? '參考匯率折算' : '参考汇率折算'}: 1 {snapshot.quote_currency} = {snapshot.fx_rate.toPrecision(6)} {snapshot.valuation_currency || snapshot.currency} · {snapshot.fx_as_of} · {snapshot.fx_source}</p>;
  }
  return reason && reasons[reason] ? <p className="provider-hint">{reasons[reason]}</p> : null;
}
