import { CircleAlert, ListTodo, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { Language } from "../../types";
import { getResearchTaskCopy } from "./researchTasksCopy";

export type TaskNotice = {
  id: string;
  text: string;
  attention?: boolean;
  actions?: Array<{ label: string; run: () => void }>;
  dismiss: () => void;
};

function Notice({ notice, close }: { notice: TaskNotice; close: string }) {
  const [hover, setHover] = useState(false);
  const [focused, setFocused] = useState(false);
  const [visible, setVisible] = useState(() => !document.hidden);
  const left = useRef(9_000);
  const dismiss = useRef(notice.dismiss);
  dismiss.current = notice.dismiss;
  useEffect(() => {
    const update = () => setVisible(!document.hidden);
    document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, []);
  useEffect(() => {
    if (hover || focused || !visible) return;
    const started = Date.now();
    const timer = window.setTimeout(() => dismiss.current(), left.current);
    return () => {
      window.clearTimeout(timer);
      left.current = Math.max(0, left.current - (Date.now() - started));
    };
  }, [hover, focused, visible]);
  return <article className="task-toast" data-attention={notice.attention || undefined}
    onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}
    onFocusCapture={() => setFocused(true)}
    onBlurCapture={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) setFocused(false); }}>
    {notice.attention ? <CircleAlert size={17} /> : <ListTodo size={17} />}
    <div className="task-toast-content">
      <p role="status">{notice.text}</p>
      {notice.actions?.length ? <div className="task-toast-actions">{notice.actions.map((action) =>
        <button key={action.label} type="button" onClick={action.run}>{action.label}</button>)}</div> : null}
    </div>
    <button className="task-icon-action" type="button" aria-label={close} onClick={notice.dismiss}><X size={16} /></button>
  </article>;
}

export function TaskNotices({ notices, language }: { notices: TaskNotice[]; language: Language }) {
  const copy = getResearchTaskCopy(language);
  if (!notices.length) return null;
  return <section className="task-toasts" aria-label={copy.notifications}>
    {notices.slice(-3).map((notice) => <Notice key={notice.id} notice={notice} close={copy.closeNotice} />)}
  </section>;
}
