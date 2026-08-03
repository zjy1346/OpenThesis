import { useEffect, useState } from "react";

import { getThesis, listTheses, saveThesis } from "../../backend";
import { LoadingState } from "../../components/States";
import type { ThesisVersion } from "../../types";

type ThesisCopy = {
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

export function ThesisView({ copy }: { copy: ThesisCopy }) {
  const [versions, setVersions] = useState<ThesisVersion[]>([]);
  const [selected, setSelected] = useState<ThesisVersion | null>(null);
  const [editor, setEditor] = useState("");
  const [state, setState] = useState<"loading" | "idle" | "saving" | "saved" | "failed">("loading");

  useEffect(() => {
    let active = true;
    void listTheses().then((items) => {
      if (!active) return;
      setVersions(items);
      if (items[0]) {
        setSelected(items[0]);
        setEditor(JSON.stringify(items[0].content, null, 2));
      }
      setState("idle");
    }).catch(() => { if (active) setState("failed"); });
    return () => { active = false; };
  }, []);

  const choose = async (item: ThesisVersion) => {
    setState("loading");
    try {
      const full = await getThesis(item.thesis_version_id);
      setSelected(full);
      setEditor(JSON.stringify(full.content, null, 2));
      setState("idle");
    } catch {
      setState("failed");
    }
  };

  const save = async () => {
    if (!selected) return;
    let content: Record<string, unknown>;
    try {
      content = JSON.parse(editor) as Record<string, unknown>;
    } catch {
      setState("failed");
      return;
    }
    setState("saving");
    try {
      const next = await saveThesis(selected.company_cik, content);
      setSelected(next);
      setVersions(await listTheses());
      setState("saved");
    } catch {
      setState("failed");
    }
  };

  return (
    <div className="thesis-view">
      <header><span className="eyebrow">OpenThesis</span><h2>{copy.thesesTitle}</h2><p>{copy.thesesBody}</p></header>
      {state === "loading" ? <LoadingState label={copy.loading} /> : versions.length === 0 ? <p className="thesis-empty">{copy.noTheses}</p> : <div className="thesis-layout">
        <aside className="thesis-list">{versions.map((item) => <button key={item.thesis_version_id} type="button" data-selected={selected?.thesis_version_id === item.thesis_version_id || undefined} onClick={() => void choose(item)}><strong>{item.ticker} · v{item.version}</strong><span>{item.name}</span><small>{new Date(item.created_at).toLocaleString()}</small></button>)}</aside>
        <section className="thesis-editor"><label htmlFor="thesis-json">{copy.thesisJson}</label><textarea id="thesis-json" spellCheck={false} value={editor} onChange={(event) => { setEditor(event.target.value); setState("idle"); }} /><button className="primary-button" type="button" onClick={() => void save()} disabled={state === "saving"}>{state === "saving" ? copy.saving : copy.saveNewVersion}</button>{state === "saved" && <p className="settings-message" role="status">{copy.saved}</p>}{state === "failed" && <p className="inline-error" role="alert">{copy.invalidJson}</p>}</section>
      </div>}
    </div>
  );
}
