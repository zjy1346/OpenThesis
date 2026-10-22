import { useEffect, useState } from "react";
import { getBackendInitializationStatus } from "../backend";
import { formatElapsedTime } from "../features/research/researchProgress";

const COPY = {
  en: {
    initializing: "Initializing the research core", waiting: "Preparing local data; the window remains available. History will load separately.", backupPages: "Backup pages",
    stages: { checking_schema: "Checking database schema", checking_integrity: "Checking data integrity", backing_up: "Backing up local data", migrating_schema: "Migrating database schema", backfilling_facts: "Backfilling financial facts", building_read_models: "Building research views", storage_ready: "Local storage ready", preparing_services: "Preparing local services", ready: "Ready" },
  },
  "zh-CN": {
    initializing: "正在初始化研究核心", waiting: "正在准备本地数据，窗口仍可操作；历史报告将单独加载。", backupPages: "备份页数",
    stages: { checking_schema: "检查数据库结构", checking_integrity: "校验数据完整性", backing_up: "备份本地数据", migrating_schema: "迁移数据库结构", backfilling_facts: "回填财务事实", building_read_models: "构建研究视图", storage_ready: "本地存储已就绪", preparing_services: "准备本地服务", ready: "已就绪" },
  },
  "zh-Hant": {
    initializing: "正在初始化研究核心", waiting: "正在準備本地資料，視窗仍可操作；歷史報告將另行載入。", backupPages: "備份頁數",
    stages: { checking_schema: "檢查資料庫結構", checking_integrity: "檢查資料完整性", backing_up: "備份本地資料", migrating_schema: "遷移資料庫結構", backfilling_facts: "回填財務事實", building_read_models: "建立研究檢視", storage_ready: "本地儲存已就緒", preparing_services: "準備本地服務", ready: "已就緒" },
  },
};

export function CoreStartup({ language }: { language: string }) {
  const [elapsed, setElapsed] = useState(0);
  const [snapshot, setSnapshot] = useState<Awaited<ReturnType<typeof getBackendInitializationStatus>> | null>(null);
  useEffect(() => {
    let active = true;
    let inFlight = false;
    const started = performance.now();
    const timer = window.setInterval(() => {
      setElapsed(Math.floor((performance.now() - started) / 1000));
      if (inFlight) return;
      inFlight = true;
      void (async () => {
        try {
          const status = await getBackendInitializationStatus();
          if (active && status) setSnapshot(status);
        } catch { /* Bootstrap owns connection errors; this never restarts it. */ }
        finally { inFlight = false; }
      })();
    }, 500);
    return () => { active = false; window.clearInterval(timer); };
  }, []);
  const copy = COPY[language === "en" ? "en" : language === "zh-Hant" ? "zh-Hant" : "zh-CN"];
  const stage = snapshot?.stage ? copy.stages[snapshot.stage as keyof typeof copy.stages] : undefined;
  const backupProgress = snapshot?.stage === "backing_up"
    && Number.isInteger(snapshot.completed_units) && Number.isInteger(snapshot.total_units)
    && (snapshot.completed_units ?? -1) >= 0 && (snapshot.total_units ?? 0) > 0
    && (snapshot.completed_units ?? Number.MAX_SAFE_INTEGER) <= (snapshot.total_units ?? -1)
    ? `${copy.backupPages}: ${snapshot.completed_units} / ${snapshot.total_units}` : undefined;
  return <div className="bootstrap-shell" role="status" aria-label="OpenThesis" aria-busy="true">
    <strong>OpenThesis</strong>
    {elapsed > 0 && <><span>{copy.initializing}</span><time>{formatElapsedTime(elapsed)}</time></>}
    {stage && <small className="bootstrap-stage" aria-live="polite">{stage}</small>}
    {backupProgress && <small className="bootstrap-stage-progress">{backupProgress}</small>}
    {!stage && snapshot?.phase === "starting" && elapsed >= 3 && <small>{copy.waiting}</small>}
  </div>;
}
