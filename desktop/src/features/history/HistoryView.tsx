import { useState } from "react";
import { ArrowUpRight, CalendarDays, RefreshCw } from "lucide-react";

import type { Language, ResearchRunSummary } from "../../types";

type HistoryCopy = {
  historyBody: string;
  historyCount: string;
  refreshHistory: string;
  refreshingHistory: string;
  noHistory: string;
  viewReport: string;
};

export function HistoryView({ runs, language, copy, onRefresh, onSelect }: {
  runs: ResearchRunSummary[];
  language: Language;
  copy: HistoryCopy;
  onRefresh: () => Promise<void>;
  onSelect: (run: ResearchRunSummary) => Promise<void>;
}) {
  const [refreshing, setRefreshing] = useState(false);

  const refresh = async () => {
    setRefreshing(true);
    try {
      await onRefresh();
    } finally {
      setRefreshing(false);
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
            <button className="history-card" key={run.run_id} type="button" onClick={() => void onSelect(run)}>
              <span className="history-symbol">{run.ticker || "—"}</span>
              <span className="history-card-main">
                <strong>{run.company_name}</strong>
                <small><CalendarDays size={13} />{new Date(run.started_at).toLocaleString(language)}</small>
              </span>
              <span className="history-card-status" data-status={run.status}>{run.status}</span>
              <span className="history-card-action">{copy.viewReport}<ArrowUpRight size={15} /></span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
