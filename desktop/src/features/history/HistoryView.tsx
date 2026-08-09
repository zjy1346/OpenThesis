import { useState } from "react";
import { ArrowUpRight, CalendarDays, RefreshCw, Trash2 } from "lucide-react";

import type { Language, ResearchRunSummary } from "../../types";

type HistoryCopy = {
  historyBody: string;
  historyCount: string;
  refreshHistory: string;
  refreshingHistory: string;
  noHistory: string;
  viewReport: string;
  deleteResearch: string;
  deleteResearchTitle: string;
  deleteResearchBody: string;
  deleteResearchConfirm: string;
  deleteResearchCancel: string;
  deletingResearch: string;
  deleteResearchFailed: string;
};

export function HistoryView({ runs, language, copy, onRefresh, onSelect, onDelete }: {
  runs: ResearchRunSummary[];
  language: Language;
  copy: HistoryCopy;
  onRefresh: () => Promise<void>;
  onSelect: (run: ResearchRunSummary) => Promise<void>;
  onDelete: (run: ResearchRunSummary) => Promise<void>;
}) {
  const [refreshing, setRefreshing] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<ResearchRunSummary | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState("");

  const refresh = async () => {
    setRefreshing(true);
    try {
      await onRefresh();
    } finally {
      setRefreshing(false);
    }
  };

  const confirmDelete = async () => {
    if (!pendingDelete) return;
    setDeleting(true);
    setDeleteError("");
    try {
      await onDelete(pendingDelete);
      setPendingDelete(null);
    } catch {
      setDeleteError(copy.deleteResearchFailed);
    } finally {
      setDeleting(false);
    }
  };

  return (
    <div className="history-view">
      <header className="section-intro">
        <div>
          <p>{copy.historyBody}</p>
          <span>{copy.historyCount.replace("{count}", String(runs.length))}</span>
        </div>
        <button className="secondary-button compact-action" type="button" disabled={refreshing} onClick={() => void refresh()}>
          <RefreshCw size={15} />{refreshing ? copy.refreshingHistory : copy.refreshHistory}
        </button>
      </header>
      {runs.length === 0 ? <div className="history-empty">{copy.noHistory}</div> : (
        <div className="history-grid">
          {runs.map((run) => (
            <article className="history-card" key={run.run_id}>
              <button className="history-card-open" type="button" onClick={() => void onSelect(run)}>
                <span className="history-symbol">{run.ticker || "—"}</span>
                <span className="history-card-main">
                  <strong>{run.company_name}</strong>
                  <small>{run.market || "US"}{run.exchange ? ` · ${run.exchange}` : ""}</small>
                  <small><CalendarDays size={13} />{new Date(run.started_at).toLocaleString(language)}</small>
                </span>
              </button>
              <span className="history-card-status" data-status={run.status}>{run.status}</span>
              <span className="history-card-action">{copy.viewReport}<ArrowUpRight size={15} /></span>
              <button className="history-delete" type="button" aria-label={`${copy.deleteResearch}: ${run.company_name}`}
                title={copy.deleteResearch} onClick={() => { setDeleteError(""); setPendingDelete(run); }}>
                <Trash2 size={15} />
              </button>
            </article>
          ))}
        </div>
      )}
      {pendingDelete && (
        <div className="history-dialog-backdrop" role="presentation" onMouseDown={(event) => {
          if (event.target === event.currentTarget && !deleting) setPendingDelete(null);
        }}>
          <div className="history-dialog" role="dialog" aria-modal="true" aria-labelledby="delete-history-title">
            <div className="history-dialog-icon"><Trash2 size={19} /></div>
            <h2 id="delete-history-title">{copy.deleteResearchTitle}</h2>
            <p>{copy.deleteResearchBody
              .replace("{company}", pendingDelete.company_name)
              .replace("{ticker}", pendingDelete.ticker || "—")}</p>
            <small>{new Date(pendingDelete.started_at).toLocaleString(language)}</small>
            {deleteError && <div className="history-dialog-error" role="alert">{deleteError}</div>}
            <div className="history-dialog-actions">
              <button className="secondary-button" type="button" disabled={deleting} onClick={() => setPendingDelete(null)}>{copy.deleteResearchCancel}</button>
              <button className="danger-button" type="button" disabled={deleting} onClick={() => void confirmDelete()}>{deleting ? copy.deletingResearch : copy.deleteResearchConfirm}</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
