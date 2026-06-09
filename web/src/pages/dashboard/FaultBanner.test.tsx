import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { SafetyEvaluationData } from "./events";
import { FaultBanner } from "./FaultBanner";
import type { SafetyTrailEntry } from "./useDashboardEvents";

afterEach(cleanup);

const FAULT: SafetyEvaluationData = {
  rule: "env_temp_ceiling",
  verdict: "fault",
  input_heat: null,
  input_fan: null,
  adjusted_heat: 0,
  adjusted_fan: 100,
  reason: "Environment temp exceeded the 240 °C ceiling",
};

const TRAIL: SafetyTrailEntry[] = [
  {
    kind: "safety_alert",
    evaluation: { ...FAULT, verdict: "emergency_stop", reason: "env ceiling tripped emergency stop" },
  },
  { kind: "fault", evaluation: FAULT },
];

describe("FaultBanner", () => {
  it("renders nothing when there is no fault", () => {
    render(<FaultBanner fault={null} trail={[]} onAcknowledge={() => {}} canAcknowledge />);
    expect(screen.queryByTestId("fault-banner")).toBeNull();
  });

  it("states what the safety layer did (the fault reason)", () => {
    render(<FaultBanner fault={FAULT} trail={[]} onAcknowledge={() => {}} canAcknowledge />);
    expect(screen.getByTestId("fault-reason")).toHaveTextContent(/exceeded the 240 °C ceiling/);
  });

  it("shows the forced-safe state (heat forced to 0, fan safe)", () => {
    render(<FaultBanner fault={FAULT} trail={[]} onAcknowledge={() => {}} canAcknowledge />);
    const banner = screen.getByTestId("fault-banner");
    expect(banner).toHaveTextContent(/0 % \(forced\)/);
    expect(banner).toHaveTextContent(/100 % \(safe\)/);
  });

  it("accumulates the safety event trail with verdict labels", () => {
    render(<FaultBanner fault={FAULT} trail={TRAIL} onAcknowledge={() => {}} canAcknowledge />);
    const rows = screen.getAllByTestId("safety-trail-row");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("EMERGENCY STOP");
    expect(rows[1]).toHaveTextContent("FAULT");
  });

  it("fires acknowledge when permitted", () => {
    const onAcknowledge = vi.fn();
    render(<FaultBanner fault={FAULT} trail={[]} onAcknowledge={onAcknowledge} canAcknowledge />);
    fireEvent.click(screen.getByTestId("acknowledge-fault"));
    expect(onAcknowledge).toHaveBeenCalledOnce();
  });

  it("disables acknowledge when not permitted", () => {
    const onAcknowledge = vi.fn();
    render(
      <FaultBanner fault={FAULT} trail={[]} onAcknowledge={onAcknowledge} canAcknowledge={false} />,
    );
    const button = screen.getByTestId("acknowledge-fault");
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(onAcknowledge).not.toHaveBeenCalled();
  });
});
