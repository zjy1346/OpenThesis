import { describe, expect, it } from "vitest";

import {
  formatElapsedTime,
  progressStageCopy,
  waitingMessageAt,
  WAITING_RANDOM_COUNT,
} from "./researchProgress";

describe("research progress experience", () => {
  it("formats a monotonic count-up timer", () => {
    expect(formatElapsedTime(0)).toBe("00:00");
    expect(formatElapsedTime(198)).toBe("03:18");
    expect(formatElapsedTime(3_661)).toBe("01:01:01");
  });

  it("shows the two fixed messages for ten seconds each", () => {
    expect(waitingMessageAt("zh-CN", 0, "job-1")).toContain("巴菲特用一生");
    expect(waitingMessageAt("zh-CN", 9, "job-1")).toContain("巴菲特用一生");
    expect(waitingMessageAt("zh-CN", 10, "job-1")).toContain("真金白银");
    expect(waitingMessageAt("zh-CN", 19, "job-1")).toContain("真金白银");
  });

  it("uses a no-repeat shuffled cycle after twenty seconds", () => {
    const cycle = Array.from(
      { length: WAITING_RANDOM_COUNT },
      (_, index) => waitingMessageAt("en", 20 + index * 5, "job-random"),
    );
    expect(new Set(cycle)).toHaveLength(WAITING_RANDOM_COUNT);
    expect(waitingMessageAt("en", 20 + (WAITING_RANDOM_COUNT - 1) * 5, "job-random"))
      .not.toBe(waitingMessageAt("en", 20 + WAITING_RANDOM_COUNT * 5, "job-random"));
  });

  it("localizes stable backend stage identifiers", () => {
    expect(progressStageCopy("zh-CN", "filing-parse").title).toBe("正在识别财务报表……");
    expect(progressStageCopy("en", "synthesis").title).toBe("Integrating the investment conclusion…");
    expect(progressStageCopy("zh-CN", "internal-unknown").title).not.toContain("internal-unknown");
  });

  it("has complete English copy for every public stage and waiting message", () => {
    const stages = [
      "preparing", "company-profile", "filing-discovery", "filing-download",
      "filing-parse", "filing-validation", "vision-approval", "vision-processing",
      "financial-analysis", "business-analysis", "risk-analysis", "growth-analysis",
      "counter-analysis", "scenario-analysis", "synthesis", "comparison", "cancelling",
    ];
    for (const stage of stages) {
      const copy = progressStageCopy("en", stage);
      expect(copy.title).not.toMatch(/[\u3400-\u9fff]/);
      expect(copy.note).not.toMatch(/[\u3400-\u9fff]/);
    }
    const messages = [
      waitingMessageAt("en", 0, "english"),
      waitingMessageAt("en", 10, "english"),
      ...Array.from(
        { length: WAITING_RANDOM_COUNT },
        (_, index) => waitingMessageAt("en", 20 + index * 5, "english"),
      ),
    ];
    expect(messages).toHaveLength(23);
    expect(messages.every((message) => !/[\u3400-\u9fff]/.test(message))).toBe(true);
  });
});
