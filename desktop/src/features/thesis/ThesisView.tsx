import { useEffect, useMemo, useRef, useState } from "react";

import { getThesis, listTheses, saveThesis } from "../../backend";
import { LoadingState } from "../../components/States";
import type { ThesisVersion, Language } from "../../types";
import { getThesisWorkbenchCopy } from "./thesisCopy";
import "./ThesisView.css";
import { ThesisEvidence } from './ThesisEvidence';
import {
  applyThesisDraft,
  diffThesisVersions,
  formatDiffValue,
  readThesisDraft,
  type ThesisDiffEntry,
  type ThesisDraft,
} from "./thesisModel";

export type ThesisCopy = {
  thesesTitle: string;
  thesesBody: string;
  noTheses: string;
  thesisJson: string;
  saveNewVersion: string;
  saving: string;
  saved: string;
  invalidJson: string;
  loading: string;
};

type ViewState =
  | "loading"
  | "ready"
  | "saving"
  | "saved"
  | "fetch-error"
  | "version-fetch-error"
  | "validation-error"
  | "save-error";

const EMPTY_DRAFT: ThesisDraft = {
  coreThesis: "",
  keyAssumptions: "",
  supportingEvidence: "",
  counterEvidence: "",
  invalidationConditions: "",
  leadingIndicators: "",
  changeReason: "",
};

function sameDraft(left: ThesisDraft, right: ThesisDraft): boolean {
  return Object.keys(EMPTY_DRAFT).every((key) => left[key as keyof ThesisDraft] === right[key as keyof ThesisDraft]);
}

function previousVersion(versions: ThesisVersion[], selected: ThesisVersion): ThesisVersion | undefined {
  const parent = versions.find(item => item.company_cik === selected.company_cik && item.thesis_version_id === selected.content._parent_thesis_version_id);
  if (parent) return parent;
  return versions
    .filter((item) => item.company_cik === selected.company_cik && item.version < selected.version)
    .sort((left, right) => right.version - left.version)[0];
}

function diffLabel(copy: ReturnType<typeof getThesisWorkbenchCopy>, kind: ThesisDiffEntry["kind"]): string {
  return kind === "added" ? copy.added : kind === "removed" ? copy.removed : copy.changed;
}

export function ThesisView({ copy, language }: { copy: ThesisCopy; language?: Language }) {
  const localCopy = getThesisWorkbenchCopy(copy, language);
  const [versions, setVersions] = useState<ThesisVersion[]>([]);
  const [selected, setSelected] = useState<ThesisVersion | null>(null);
  const [draft, setDraft] = useState<ThesisDraft>(EMPTY_DRAFT);
  const [initialDraft, setInitialDraft] = useState<ThesisDraft>(EMPTY_DRAFT);
  const [state, setState] = useState<ViewState>("loading");
  const [pendingSelection, setPendingSelection] = useState<ThesisVersion | null>(null);
  const [baselineFull, setBaselineFull] = useState<ThesisVersion | null>(null);
  const [baselineFailed, setBaselineFailed] = useState(false);
  const loadSequence = useRef(0);
  const saveSequence = useRef(0);

  const dirty = Boolean(selected) && !sameDraft(draft, initialDraft);
  const baseline = selected ? previousVersion(versions, selected) : undefined;
  useEffect(() => {
    let active = true;
    setBaselineFull(null);
    setBaselineFailed(false);
    if (baseline) void getThesis(baseline.thesis_version_id).then(item => {
      if (active && item.thesis_version_id === baseline.thesis_version_id) setBaselineFull(item);
    }).catch(() => { if (active) setBaselineFailed(true); });
    return () => { active = false; };
  }, [baseline?.thesis_version_id]);
  const selectedChangeReason = selected && typeof selected.content.change_reason === "string" ? selected.content.change_reason : "";
  const diffs = useMemo(() => {
    if (!selected || !baseline) return [];
    return [...diffThesisVersions(baseline.content, selected.content),
      ...(baselineFull?.sources && selected.sources ? diffThesisVersions({ supporting_evidence_ids: baselineFull.sources }, { supporting_evidence_ids: selected.sources }) : [])];
  }, [baseline, baselineFull, selected]);

  const hydrate = (item: ThesisVersion) => {
    const nextDraft = { ...readThesisDraft(item.content), changeReason: '' };
    setSelected(item);
    setDraft(nextDraft);
    setInitialDraft(nextDraft);
    setPendingSelection(null);
  };

  const openVersion = async (item: ThesisVersion) => {
    const sequence = ++loadSequence.current;
    setState("loading");
    try {
      const full = await getThesis(item.thesis_version_id);
      if (sequence !== loadSequence.current) return;
      hydrate(full);
      setState("ready");
    } catch {
      if (sequence === loadSequence.current) setState("version-fetch-error");
    }
  };

  useEffect(() => {
    const sequence = ++loadSequence.current;
    let active = true;
    setState("loading");
    void listTheses().then((items) => {
      if (!active || sequence !== loadSequence.current) return;
      setVersions(items);
      if (items[0]) void openVersion(items[0]);
      else setState("ready");
    }).catch(() => {
      if (!active || sequence !== loadSequence.current) return;
      setVersions([]);
      setSelected(null);
      setState("fetch-error");
    });
    return () => {
      active = false;
      loadSequence.current += 1;
      saveSequence.current += 1;
    };
  }, []);

  const choose = (item: ThesisVersion) => {
    if (selected?.thesis_version_id === item.thesis_version_id) return;
    if (dirty) {
      setPendingSelection(item);
      return;
    }
    void openVersion(item);
  };

  const updateDraft = (field: keyof ThesisDraft, value: string) => {
    setDraft((current) => ({ ...current, [field]: value }));
    if (state !== "saving") setState("ready");
  };

  const save = async () => {
    if (!selected || state === "saving") return;
    if (!draft.changeReason.trim()) {
      setState("validation-error");
      return;
    }
    const sequence = ++saveSequence.current;
    const selectedId = selected.thesis_version_id;
    const content = applyThesisDraft(selected.content, draft, initialDraft);
    content.change_reason = draft.changeReason;
    setState("saving");
    try {
      const saved = await saveThesis(selected.company_cik, content, selected.thesis_version_id);
      const next: ThesisVersion = {
        ...saved,
        ticker: saved.ticker || selected.ticker,
        name: saved.name || selected.name,
      };
      if (sequence !== saveSequence.current || selectedId !== selected.thesis_version_id) return;
      let refreshed: ThesisVersion[] = [];
      try {
        refreshed = await listTheses();
      } catch {
        // The append-only save already succeeded. Keep the returned version visible.
      }
      if (sequence !== saveSequence.current) return;
      setVersions((current) => {
        const source = refreshed.length ? refreshed : current;
        return [next, ...source.filter((item) => item.thesis_version_id !== next.thesis_version_id)];
      });
      hydrate(next);
      setState("saved");
    } catch {
      if (sequence === saveSequence.current) setState("save-error");
    }
  };

  const statusMessage = state === "fetch-error"
    ? localCopy.loadFailed
    : state === "version-fetch-error"
      ? localCopy.fetchVersionFailed
      : state === "validation-error"
        ? localCopy.validationFailed
        : state === "save-error"
          ? localCopy.saveFailed
          : undefined;

  return (
    <div className="thesis-view">
      <header>
        <span className="eyebrow">OpenThesis</span>
        <h2>{copy.thesesTitle}</h2>
        <p>{copy.thesesBody}</p>
      </header>
      {state === "loading" && !selected ? <LoadingState label={copy.loading} /> : state === "fetch-error" ? <p className="inline-error" role="alert">{statusMessage}</p> : state === "version-fetch-error" && !selected ? <p className="inline-error" role="alert">{statusMessage}</p> : versions.length === 0 ? <p className="thesis-empty">{copy.noTheses}</p> : (
        <div className="thesis-layout">
          <aside className="thesis-list" aria-label={copy.thesesTitle}>
            {versions.map((item) => (
              <button key={item.thesis_version_id} type="button" disabled={state === 'saving'} data-selected={selected?.thesis_version_id === item.thesis_version_id || undefined} onClick={() => choose(item)}>
                <strong>{item.ticker} · v{item.version}</strong>
                <span>{item.name}</span>
                <small>{new Date(item.created_at).toLocaleString()}</small>
              </button>
            ))}
          </aside>
          <section className="thesis-editor" aria-label={copy.thesesTitle}>
            {selected && <fieldset className="thesis-edit-fields" disabled={state === 'saving' || state === 'loading'}>
              <div className="thesis-version-meta">
                <strong>{selected.ticker} · v{selected.version}</strong>
                <span>{selected.name}</span>
              </div>
              {state === "version-fetch-error" && <p className="inline-error" role="alert">{statusMessage}</p>}
              <p className="thesis-legacy-note">{localCopy.legacyDataNote}</p>
              <div className="thesis-structured-grid">
                <label className="thesis-field thesis-field-wide" htmlFor="thesis-core">
                  <span>{localCopy.coreThesis}</span>
                  <textarea id="thesis-core" value={draft.coreThesis} onChange={(event) => updateDraft("coreThesis", event.target.value)} />
                </label>
                <label className="thesis-field" htmlFor="thesis-assumptions">
                  <span>{localCopy.keyAssumptions}</span>
                  <textarea id="thesis-assumptions" value={draft.keyAssumptions} onChange={(event) => updateDraft("keyAssumptions", event.target.value)} />
                </label>
                <label className="thesis-field" htmlFor="thesis-supporting">
                  <span>{localCopy.supportingEvidence} · {localCopy.evidenceSource}</span>
                  <textarea id="thesis-supporting" value={draft.supportingEvidence} aria-describedby="thesis-source-hint" onChange={(event) => updateDraft("supportingEvidence", event.target.value)} />
                </label>
                <p id="thesis-source-hint" className="thesis-field-hint">{localCopy.sourceIdsHint}</p>
                <label className="thesis-field" htmlFor="thesis-counter">
                  <span>{localCopy.counterEvidence} · {localCopy.evidenceSource}</span>
                  <textarea id="thesis-counter" value={draft.counterEvidence} onChange={(event) => updateDraft("counterEvidence", event.target.value)} />
                </label>
                <label className="thesis-field" htmlFor="thesis-invalidation">
                  <span>{localCopy.invalidationConditions}</span>
                  <textarea id="thesis-invalidation" value={draft.invalidationConditions} onChange={(event) => updateDraft("invalidationConditions", event.target.value)} />
                </label>
                <label className="thesis-field" htmlFor="thesis-indicators">
                  <span>{localCopy.leadingIndicators}</span>
                  <textarea id="thesis-indicators" value={draft.leadingIndicators} onChange={(event) => updateDraft("leadingIndicators", event.target.value)} />
                </label>
              </div>
              <label className="thesis-field thesis-reason" htmlFor="thesis-change-reason">
                <span>{localCopy.changeReason}</span>
                <input id="thesis-change-reason" aria-label={localCopy.changeReason} value={draft.changeReason} onChange={(event) => updateDraft("changeReason", event.target.value)} />
                <small>{localCopy.changeReasonHint}</small>
              </label>
              <div className="thesis-actions">
                <button className="primary-button" type="button" onClick={() => void save()} disabled={state === "saving"}>{state === "saving" ? copy.saving : copy.saveNewVersion}</button>
                {state === "saved" && <p className="settings-message" role="status">{copy.saved}</p>}
              </div>
              {statusMessage && state !== "version-fetch-error" && <p className="inline-error" role="alert">{statusMessage}</p>}
              {pendingSelection && <div className="thesis-dirty-guard" role="dialog" aria-label={localCopy.dirtyWarning}>
                <p>{localCopy.dirtyWarning}</p>
                <button type="button" onClick={() => { const next = pendingSelection; setPendingSelection(null); void openVersion(next); }}>{localCopy.discardChanges}</button>
                <button type="button" onClick={() => setPendingSelection(null)}>{localCopy.keepEditing}</button>
              </div>}
              <ThesisEvidence thesis={selected} labels={localCopy} />
              <section className="thesis-diff" aria-label={localCopy.versionDiff}>
                {baselineFailed && <p className="inline-error" role="alert">{localCopy.baselineFailed}</p>}
                <div className="thesis-section-heading"><h3>{localCopy.versionDiff}</h3><span>{baseline ? `${localCopy.previousVersion} v${baseline.version} → ${localCopy.currentVersion} v${selected.version}` : ""}</span></div>
                {selectedChangeReason && <p className="thesis-change-reason"><strong>{localCopy.reasonPrefix}:</strong> {selectedChangeReason}</p>}
                {diffs.length === 0 ? <p className="thesis-empty">{localCopy.noChanges}</p> : <ul>{diffs.map((entry, index) => <li key={`${entry.kind}:${entry.path}:${index}`} data-kind={entry.kind}><strong>{diffLabel(localCopy, entry.kind)}</strong><span>{({ thesis: localCopy.coreThesis, key_assumptions: localCopy.keyAssumptions, assumptions: localCopy.keyAssumptions, supporting_evidence_ids: localCopy.supportingEvidence, counter_evidence_ids: localCopy.counterEvidence, invalidation_conditions: localCopy.invalidationConditions, leading_indicators: localCopy.leadingIndicators, change_reason: localCopy.changeReason } as Record<string, string>)[entry.path.replace('[]', '')] || localCopy.changed}</span><span>{entry.kind !== "added" && formatDiffValue(entry.before)}</span><span>{entry.kind === "changed" && " → "}</span><span>{entry.kind !== "removed" && formatDiffValue(entry.after)}</span></li>)}</ul>}
              </section>
            </fieldset>}
          </section>
        </div>
      )}
    </div>
  );
}
