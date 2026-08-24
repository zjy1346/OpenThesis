import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { HistoryView } from "./HistoryView";


describe("history deletion", () => {
  it("requires confirmation and deletes only the selected run", async () => {
    const onDelete = vi.fn().mockResolvedValue(undefined);
    render(
      <HistoryView
        runs={[{
          run_id: "run-delete",
          ticker: "688981.SH",
          company_name: "中芯国际",
          status: "completed",
          started_at: "2026-08-09T10:00:00+08:00",
          completed_at: "2026-08-09T10:05:00+08:00",
          report_language: "zh-CN",
          market: "CN_A",
          exchange: "SSE",
          listing_currency: "CNY",
        }]}
        language="zh-CN"
        copy={{
          historyBody: "历史",
          historyCount: "共 {count} 份报告",
          refreshHistory: "刷新",
          refreshingHistory: "刷新中",
          noHistory: "暂无",
          viewReport: "查看报告",
          deleteResearch: "删除研究记录",
          deleteResearchTitle: "删除这份研究记录？",
          deleteResearchBody: "删除 {company}（{ticker}）",
          deleteResearchConfirm: "确认删除",
          deleteResearchCancel: "取消",
          deletingResearch: "正在删除",
          deleteResearchFailed: "删除失败",
          running: "运行中",
          queued: "等待中",
          completed: "已完成",
          cancelled: "已取消",
          failed: "失败",
          partial: "部分完成",
        }}
        onRefresh={vi.fn().mockResolvedValue(undefined)}
        onSelect={vi.fn().mockResolvedValue(undefined)}
        onDelete={onDelete}
      />,
    );

    expect(screen.getByText("已完成")).toBeVisible();
    const deleteButton = screen.getByRole("button", { name: "删除研究记录: 中芯国际" });
    fireEvent.click(deleteButton);
    expect(screen.getByRole("dialog")).toHaveTextContent("中芯国际（688981.SH）");
    expect(onDelete).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "取消" })).toHaveFocus();

    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await waitFor(() => expect(deleteButton).toHaveFocus());
    fireEvent.click(deleteButton);

    fireEvent.click(screen.getByRole("button", { name: "确认删除" }));
    await waitFor(() => expect(onDelete).toHaveBeenCalledWith(expect.objectContaining({ run_id: "run-delete" })));
  });
});
