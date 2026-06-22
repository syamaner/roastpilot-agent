import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { SafetyVerdict } from "@/lib/types";
import { AdvisoryPanel } from "./AdvisoryPanel";
import type { AdvisoryRecord } from "./useDashboardEvents";

afterEach(cleanup);

let seq = 0;
function record(
  verdict: SafetyVerdict,
  heat = 60,
  atServeSeconds: number | null = 1010,
  beanTempC: number | null = 203,
): AdvisoryRecord {
  return {
    seq: seq++,
    decision: { target_heat: heat, target_fan: 75, should_drop: false, confidence: 0.82, rationale: "RoR declining; reduce heat" },
    evaluation: {
      rule: "rate_limit",
      verdict,
      input_heat: heat,
      input_fan: 75,
      adjusted_heat: heat,
      adjusted_fan: 75,
      reason: "ok",
    },
    synthesized: false,
    atServeSeconds,
    beanTempC,
  };
}

describe("AdvisoryPanel", () => {
  it("renders the empty state before any recommendation", () => {
    render(<AdvisoryPanel latest={null} history={[]} paused={false} />);
    expect(screen.getByTestId("advisory-empty")).toBeInTheDocument();
  });

  it.each([
    ["allow", "ALLOW"],
    ["clamp", "CLAMP"],
    ["reject", "REJECT"],
  ] as const)("renders the %s verdict badge with the enum label", (verdict, label) => {
    render(<AdvisoryPanel latest={record(verdict)} history={[]} paused={false} />);
    const badge = screen.getAllByTestId("verdict-badge")[0];
    expect(badge).toHaveTextContent(label);
    // Never the prototype's ACCEPT.
    expect(badge).not.toHaveTextContent("ACCEPT");
  });

  it("renders the recommended heat/fan and rationale", () => {
    render(<AdvisoryPanel latest={record("allow", 60)} history={[]} paused={false} />);
    const latest = screen.getByTestId("advisory-latest");
    expect(latest).toHaveTextContent("60 %");
    expect(latest).toHaveTextContent("75 %");
    expect(latest).toHaveTextContent(/reduce heat/i);
  });

  it("renders the decision-history rows with their badges", () => {
    const history = [record("clamp"), record("allow"), record("reject")];
    render(<AdvisoryPanel latest={history[0]} history={history} paused={false} />);
    expect(screen.getAllByTestId("advisory-history-row")).toHaveLength(3);
  });

  it("renders charge-referenced roast-time + bean-temp on the latest card (#325)", () => {
    // The LatestRecommendation sub-component renders advisory-latest-context with the
    // same formatRoastTime / formatTempC calls — serve 1010 − origin 530 = 480 s = 8:00.
    const rec = record("allow", 0, 1010, 203);
    render(<AdvisoryPanel latest={rec} history={[rec]} paused={false} originSeconds={530} />);
    const ctx = screen.getByTestId("advisory-latest-context");
    expect(ctx).toHaveTextContent("8:00");
    expect(ctx).toHaveTextContent("203.0 °C");
  });

  it("renders charge-referenced roast-time + bean-temp per history row (#325)", () => {
    // Each row stamps its serve-elapsed + bean-temp; with the charge origin
    // (originSeconds) the time renders CHARGE-referenced (same M:SS as the chart):
    // serve 1010 − origin 530 = 480 s = 8:00. Bean-temp is the row's reading, °C.
    const history = [record("allow", 0, 1010, 203)];
    render(
      <AdvisoryPanel latest={history[0]} history={history} paused={false} originSeconds={530} />,
    );
    const ctx = screen.getAllByTestId("advisory-history-context")[0];
    expect(ctx).toHaveTextContent("8:00");
    expect(ctx).toHaveTextContent("203.0 °C");
  });

  it("rows are DISTINGUISHABLE — different roast-moments render different context (#325)", () => {
    // The whole point of #325: four "Heat 0% · Fan 75% · ALLOW" rows must no longer
    // be identical. Distinct serve-times/temps must produce distinct context cells.
    const history = [record("allow", 0, 1010, 203), record("allow", 0, 950, 195)];
    render(
      <AdvisoryPanel latest={history[0]} history={history} paused={false} originSeconds={530} />,
    );
    const cells = screen.getAllByTestId("advisory-history-context").map((c) => c.textContent);
    expect(cells[0]).not.toBe(cells[1]);
    expect(cells[0]).toContain("8:00"); // 1010 − 530
    expect(cells[1]).toContain("7:00"); // 950 − 530
  });

  it("falls back to serve-elapsed when the charge origin is unknown (pre-T0, #325)", () => {
    // Before T0 (origin null) formatRoastTime shows serve-elapsed, not a charge
    // offset — consistent with the chart's pre-charge axis behavior (#326).
    const history = [record("allow", 0, 300, 95)];
    render(<AdvisoryPanel latest={history[0]} history={history} paused={false} />);
    const ctx = screen.getAllByTestId("advisory-history-context")[0];
    expect(ctx).toHaveTextContent("5:00"); // serve-elapsed 300 s, no origin subtracted
    expect(ctx).toHaveTextContent("95.0 °C");
  });

  it("shows a placeholder context when an advisory has no stamped reading (#325)", () => {
    // An advisory folded before any telemetry has null time/temp — render the
    // formatter placeholders, never a fabricated 0:00 / 0 °C.
    const history = [record("allow", 0, null, null)];
    render(<AdvisoryPanel latest={history[0]} history={history} paused={false} originSeconds={530} />);
    const ctx = screen.getAllByTestId("advisory-history-context")[0];
    expect(ctx).toHaveTextContent("—"); // formatRoastTime null → "—"
    expect(ctx).toHaveTextContent("— °C"); // formatTempC null → "— °C"
  });

  it("shows the paused status when the advisor is paused", () => {
    render(<AdvisoryPanel latest={null} history={[]} paused={true} />);
    expect(screen.getByTestId("advisory-paused")).toBeInTheDocument();
  });

  it("flags a synthesized (replay demo) recommendation", () => {
    const r = { ...record("clamp"), synthesized: true };
    render(<AdvisoryPanel latest={r} history={[r]} paused={false} />);
    expect(screen.getByTestId("advisory-synthesized")).toBeInTheDocument();
  });
});
