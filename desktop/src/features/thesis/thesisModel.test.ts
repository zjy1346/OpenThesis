import { describe, expect, it } from 'vitest';
import { applyThesisDraft, diffThesisVersions, readThesisDraft } from './thesisModel';

describe('thesis history invariants', () => {
  it('editing string assumptions preserves structured historic entries without duplication', () => {
    const object = { text: 'Official structured assumption', provenance: { source: 'annual' } };
    const content = { thesis: 'Core', key_assumptions: ['First', object, 'Last'], extension: { keep: true } };
    const initial = readThesisDraft(content);
    expect(initial.keyAssumptions).toBe('First\nOfficial structured assumption\nLast');
    const changed = applyThesisDraft(content, { ...initial, keyAssumptions: 'Revised first\nOfficial structured assumption\nLast' }, initial);
    expect(changed.key_assumptions).toEqual(['Revised first', object, 'Last']);
    expect(changed.extension).toEqual({ keep: true });
  });
  it('renders structured evidence readably and preserves it when neighbouring text is edited', () => {
    const evidence = { evidence_id: 'fact:revenue', text: 'Revenue from annual report', provenance: { source: 'annual' } };
    const content = { thesis: 'Core', supporting_evidence: ['Manual note', evidence] };
    const initial = readThesisDraft(content);
    expect(initial.supportingEvidence).toBe('Manual note\nfact:revenue — Revenue from annual report');
    const changed = applyThesisDraft(
      content,
      { ...initial, supportingEvidence: 'Updated note\nfact:revenue — Revenue from annual report' },
      initial,
    );
    expect(changed.supporting_evidence).toEqual(['Updated note', evidence]);
  });
  it('tracks a changed source fact by stable evidence identity', () => {
    const original = { evidence_id: 'fact:revenue', concept: 'revenue', value: 100, unit: 'CNY' };
    const updated = { ...original, value: 80 };
    expect(diffThesisVersions({ evidence: [original] }, { evidence: [updated] })).toEqual([
      { path: 'evidence[]', kind: 'changed', before: original, after: updated },
    ]);
  });
});
