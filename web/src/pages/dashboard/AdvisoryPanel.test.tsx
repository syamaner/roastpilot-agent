import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { SafetyVerdict } from "@/lib/types";
import { AdvisoryPanel } from "./AdvisoryPanel";
import type { AdvisoryRecord } from "./useDashboardEvents";

afterEach(cleanup);

function record(verdict: SafetyVerdict, heat = 60): AdvisoryRecord {
  return {
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
