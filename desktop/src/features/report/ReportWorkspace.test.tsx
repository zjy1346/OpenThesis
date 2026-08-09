import { describe, expect, it } from "vitest";

import { stripReportPreamble } from "./ReportWorkspace";

describe("report presentation", () => {
  it("moves the generated Chinese report preamble out of the document body", () => {
    expect(stripReportPreamble([
      "# OpenThesis 长期公司研究",
      "",
      "研究运行：`run-123`",
      "",
      "> 本报告用于研究辅助，不构成投资建议或交易指令。",
      "",
      "# Tesla, Inc. 财务概览",
    ].join("\n"))).toBe("# Tesla, Inc. 财务概览");
  });

  it("preserves custom report content that has no generated preamble", () => {
    const markdown = "# Independent report\n\nOriginal content.";
    expect(stripReportPreamble(markdown)).toBe(markdown);
  });
});
