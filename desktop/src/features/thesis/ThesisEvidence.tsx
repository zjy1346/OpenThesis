import { openExternalUrl } from '../../backend';
import type { ThesisVersion } from '../../types';

const object = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
const list = (value: unknown): unknown[] => Array.isArray(value) ? value : value == null ? [] : [value];
const prose = (value: unknown): string => {
  if (typeof value === 'string') return value;
  const row = object(value);
  return ['text', 'description', 'summary', 'assumption', 'condition', 'indicator', 'title'].map(key => row[key]).filter(item => typeof item === 'string').join(' · ');
};

export function ThesisEvidence({ thesis, labels }: { thesis: ThesisVersion; labels: { coreThesis: string; supportingEvidence: string; counterEvidence: string; keyAssumptions: string; invalidationConditions: string; leadingIndicators: string } }) {
  const sources = new Map((thesis.sources || []).map(row => [String(row.evidence_id), row]));
  const renderSources = (values: unknown) => <ul>{list(values).map((value, index) => {
    const id = typeof value === 'string' ? value : String(object(value).evidence_id || object(value).source_id || '');
    const source = sources.get(id) || object(value);
    const text = String(source.excerpt || source.raw_text || prose(value) || id);
    const url = typeof source.source_url === 'string' && /^https:\/\//i.test(source.source_url) ? source.source_url : '';
    return <li key={`${id}:${index}`}><p>{text}</p>{source.value !== undefined && <p>{String(source.value)} {String(source.unit || '')} · {String(source.end_date || '')}</p>}
      <small>{String(source.locator || id)}</small>{url && <button type="button" className="refresh-button" onClick={() => void openExternalUrl(url)}>{String(source.title || source.concept || id)}</button>}</li>;
  })}</ul>;
  const claims = list(thesis.content.claims).filter(value => Object.keys(object(value)).length);
  const hasDirect = ['supporting_evidence_ids', 'supporting_evidence', 'counter_evidence_ids', 'counter_evidence', 'contradicting_evidence_ids'].some(key => list(thesis.content[key]).length);
  return <section className="thesis-evidence-matrix" aria-label={`${labels.supportingEvidence} / ${labels.counterEvidence}`}>
    <table><thead><tr><th>{labels.coreThesis}</th><th>{labels.supportingEvidence}</th><th>{labels.counterEvidence}</th></tr></thead>
      <tbody>{hasDirect && <tr><th>{prose(thesis.content.thesis)}</th><td>{renderSources(thesis.content.supporting_evidence_ids || thesis.content.supporting_evidence)}</td><td>{renderSources(thesis.content.counter_evidence_ids || thesis.content.counter_evidence || thesis.content.contradicting_evidence_ids)}</td></tr>}
        {claims.map((value, index) => { const row = object(value); return <tr key={String(row.claim_id || index)}><th>{prose(row)}</th><td>{renderSources(row.supporting_evidence_ids || row.evidence_ids || row.supporting_evidence)}</td><td>{renderSources(row.contradicting_evidence_ids || row.counter_evidence_ids || row.contradicting_evidence)}</td></tr>; })}</tbody></table>
    {(['key_assumptions', 'invalidation_conditions', 'leading_indicators'] as const).map((key, i) => {
      const retained = list(thesis.content[key]).filter(value => typeof value !== 'string' && prose(value));
      return retained.length ? <div key={key}><h4>{[labels.keyAssumptions, labels.invalidationConditions, labels.leadingIndicators][i]}</h4><ul>{retained.map((value, index) => <li key={index}>{prose(value)}</li>)}</ul></div> : null;
    })}
  </section>;
}
