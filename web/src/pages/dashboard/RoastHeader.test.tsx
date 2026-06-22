import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { RoastHeader } from "./RoastHeader";

afterEach(cleanup);

const BASE = {
  phase: "development" as const,
  // #308: ROAST TIME is charge-referenced. Here serve-elapsed (582) is well past
  // the charge clock (402) — they are deliberately distinct so a test that asserts
  // the wrong source fails loud.
  chargeElapsedSeconds: 402,
  elapsedSeconds: 582,
  developmentSeconds: 72,
  developmentPercent: 18.5,
  beanRorCPerMin: 8.4,
  profileName: "Ethiopian Yirgacheffe — Medium",
  firstCrack: null,
  mcpChild: "running" as const,
};

describe("RoastHeader", () => {
  it("renders the phase badge from the server phase (operator-facing label)", () => {
    render(<RoastHeader {...BASE} />);
    const badge = screen.getByTestId("phase-badge");
    expect(badge).toHaveAttribute("data-phase", "development");
    expect(badge).toHaveTextContent("DEVELOPMENT");
  });

  it("formats the roast + development timers as mm:ss (tabular)", () => {
    render(<RoastHeader {...BASE} />);
    // ROAST TIME is the CHARGE-referenced clock (#308): 402 s → 06:42, NOT the
    // serve-referenced 582 s (09:42) — proving the big clock reads since-charge.
    expect(screen.getByTestId("roast-timer")).toHaveTextContent("06:42");
    expect(screen.getByTestId("development-timer")).toHaveTextContent("01:12");
  });

  it("shows ROAST TIME as since-charge after charge, not since-serve (#308)", () => {
    render(<RoastHeader {...BASE} chargeElapsedSeconds={300} elapsedSeconds={900} />);
    // 300 s since charge → 05:00 (NOT 900 s / 15:00 since serve). No Preheat
    // read-out once charged.
    expect(screen.getByTestId("roast-timer")).toHaveTextContent("05:00");
    expect(screen.queryByTestId("preheat-timer")).toBeNull();
  });

  it("freezes ROAST TIME at the charge clock value (drop holds it, #308)", () => {
    // After drop the server stops advancing charge_elapsed_seconds; the header
    // renders whatever the frozen value is — no client-side ticking.
    const { rerender } = render(<RoastHeader {...BASE} chargeElapsedSeconds={530} />);
    expect(screen.getByTestId("roast-timer")).toHaveTextContent("08:50");
    rerender(<RoastHeader {...BASE} chargeElapsedSeconds={530} elapsedSeconds={999} />);
    expect(screen.getByTestId("roast-timer")).toHaveTextContent("08:50");
  });

  it("pre-charge shows 00:00 ROAST TIME + a distinct Preheat read-out (#308)", () => {
    // charge_elapsed_seconds null = pre-charge: the big clock reads 00:00 and the
    // serve-referenced preheat duration is surfaced SEPARATELY (not as roast time).
    render(
      <RoastHeader
        {...BASE}
        phase="preheating"
        chargeElapsedSeconds={null}
        elapsedSeconds={95}
        developmentSeconds={null}
        developmentPercent={null}
      />,
    );
    expect(screen.getByTestId("roast-timer")).toHaveTextContent("00:00");
    expect(screen.getByTestId("preheat-timer")).toHaveTextContent("01:35");
  });

  it("omits the Preheat read-out once charged (#308)", () => {
    render(<RoastHeader {...BASE} chargeElapsedSeconds={120} />);
    expect(screen.queryByTestId("preheat-timer")).toBeNull();
  });

  it("advances the Preheat read-out while preheating (live) (#330)", () => {
    // Pre-charge, live phase: the read-out tracks the server's run clock frame by
    // frame (it is server-authoritative, not a client wall-clock timer).
    const { rerender } = render(
      <RoastHeader {...BASE} phase="preheating" chargeElapsedSeconds={null} elapsedSeconds={40} />,
    );
    expect(screen.getByTestId("preheat-timer")).toHaveTextContent("00:40");
    rerender(
      <RoastHeader {...BASE} phase="preheating" chargeElapsedSeconds={null} elapsedSeconds={95} />,
    );
    expect(screen.getByTestId("preheat-timer")).toHaveTextContent("01:35");
  });

  it("FREEZES the Preheat read-out at the last live value on a fault during preheat (#330)", () => {
    // Live preheat reaches 01:35, then the server reports `faulted` (e.g. an
    // emergency stop during preheat). The controller keeps emitting frames with an
    // advancing run clock, but the read-out must HOLD the last live value (01:35),
    // not climb to the post-fault server value (03:20) — the run is stopped.
    const { rerender } = render(
      <RoastHeader {...BASE} phase="preheating" chargeElapsedSeconds={null} elapsedSeconds={95} />,
    );
    expect(screen.getByTestId("preheat-timer")).toHaveTextContent("01:35");
    rerender(
      <RoastHeader {...BASE} phase="faulted" chargeElapsedSeconds={null} elapsedSeconds={200} />,
    );
    expect(screen.getByTestId("preheat-timer")).toHaveTextContent("01:35");
    // A further advancing frame after the fault must still be ignored (frozen).
    rerender(
      <RoastHeader {...BASE} phase="faulted" chargeElapsedSeconds={null} elapsedSeconds={260} />,
    );
    expect(screen.getByTestId("preheat-timer")).toHaveTextContent("01:35");
  });

  it("FREEZES the Preheat read-out on operator-recovery during preheat (#330)", () => {
    // A restart-with-active-run lands a preheat run in recovery; the read-out holds.
    const { rerender } = render(
      <RoastHeader {...BASE} phase="preheating" chargeElapsedSeconds={null} elapsedSeconds={50} />,
    );
    expect(screen.getByTestId("preheat-timer")).toHaveTextContent("00:50");
    rerender(
      <RoastHeader
        {...BASE}
        phase="operator_recovery_required"
        chargeElapsedSeconds={null}
        elapsedSeconds={130}
      />,
    );
    expect(screen.getByTestId("preheat-timer")).toHaveTextContent("00:50");
  });

  it("freezes the diagnostics Elapsed row on a terminal phase too (#330)", () => {
    const { rerender } = render(
      <RoastHeader {...BASE} phase="preheating" chargeElapsedSeconds={null} elapsedSeconds={70} />,
    );
    rerender(
      <RoastHeader {...BASE} phase="faulted" chargeElapsedSeconds={null} elapsedSeconds={500} />,
    );
    fireEvent.click(screen.getByTestId("diagnostics-toggle"));
    // 70 s → 01:10, held; not the post-fault 500 s (08:20).
    expect(screen.getByTestId("diagnostics-drawer")).toHaveTextContent("01:10");
  });

  it("shows the live bean RoR readout in °C/min (#165)", () => {
    render(<RoastHeader {...BASE} beanRorCPerMin={8.4} />);
    const ror = screen.getByTestId("ror-readout");
    expect(ror).toHaveTextContent("8.4 °C/min");
  });

  it("shows the RoR readout from the start incl. preheat (real data, not hidden) (#165)", () => {
    // Operator clarification: pre-charge RoR is real probe data and stays shown;
    // the charge (T0) marker on the curve flags the meaningful turning point.
    render(
      <RoastHeader
        {...BASE}
        phase="preheating"
        developmentSeconds={null}
        beanRorCPerMin={14.2}
      />,
    );
    expect(screen.getByTestId("ror-readout")).toHaveTextContent("14.2 °C/min");
  });

  it("renders the RoR readout as a placeholder when no rate yet (null-safe)", () => {
    render(<RoastHeader {...BASE} beanRorCPerMin={null} />);
    expect(screen.getByTestId("ror-readout")).toHaveTextContent("— °C/min");
  });

  it("omits the development timer before first crack (GAP A — no dev% invented)", () => {
    render(<RoastHeader {...BASE} developmentSeconds={null} />);
    expect(screen.queryByTestId("development-timer")).toBeNull();
  });

  it("shows the DTR readout as a percentage (one decimal) post-FC (#220)", () => {
    render(<RoastHeader {...BASE} developmentPercent={18.5} />);
    expect(screen.getByTestId("dtr-readout")).toHaveTextContent("18.5 %");
  });

  it("renders development time and DTR as TWO DISTINCT readouts (#220)", () => {
    render(<RoastHeader {...BASE} developmentSeconds={72} developmentPercent={18.5} />);
    // The timer is mm:ss; DTR is a percent — distinct values, distinct testids.
    expect(screen.getByTestId("development-timer")).toHaveTextContent("01:12");
    expect(screen.getByTestId("dtr-readout")).toHaveTextContent("18.5 %");
  });

  it("omits the DTR readout before first crack (no DTR pre-FC) (#220)", () => {
    render(<RoastHeader {...BASE} developmentSeconds={null} developmentPercent={null} />);
    expect(screen.queryByTestId("dtr-readout")).toBeNull();
  });

  it("renders the DTR readout as a placeholder when the percent is null post-FC", () => {
    // Edge: FC fired (timer shown) but the server hasn't a DTR this frame — show "—".
    render(<RoastHeader {...BASE} developmentSeconds={72} developmentPercent={null} />);
    expect(screen.getByTestId("dtr-readout")).toHaveTextContent("— %");
  });

  it("shows FC 'listening' while roasting pre-first-crack, no mock audio dot", () => {
    render(<RoastHeader {...BASE} phase="roasting_pre_first_crack" developmentSeconds={null} />);
    const fc = screen.getByTestId("fc-status");
    expect(fc).toHaveAttribute("data-detected", "false");
    expect(fc).toHaveTextContent(/listening/i);
  });

  it("shows the real FC detection (temp + source) once it fires", () => {
    render(
      <RoastHeader {...BASE} firstCrack={{ source: "mcp", bean_temp_c: 201.2 }} />,
    );
    const fc = screen.getByTestId("fc-status");
    expect(fc).toHaveAttribute("data-detected", "true");
    expect(fc).toHaveTextContent("201.2 °C");
    expect(fc).toHaveTextContent("mcp");
  });

  it("reflects the MCP child health on the roaster-link dot", () => {
    render(<RoastHeader {...BASE} mcpChild="stopped" />);
    expect(screen.getByTestId("roaster-link")).toHaveAttribute("data-status", "stopped");
  });

  it("renders the mic-status icon from the server mic_status (#197)", () => {
    render(
      <RoastHeader
        {...BASE}
        micStatus={{
          mic_health: "ok",
          audio_running: true,
          fc_status: "pending",
          queued_window_count: 0,
          emitted_window_count: 0,
          dropped_window_count: 0,
          processed_window_count: 0,
          reason: null,
        }}
      />,
    );
    expect(screen.getByTestId("mic-status")).toHaveAttribute("data-health", "ok");
  });

  it("renders the mic icon as idle when mic_status is absent (null → idle, not red)", () => {
    render(<RoastHeader {...BASE} />);
    expect(screen.getByTestId("mic-status")).toHaveAttribute("data-health", "idle");
  });

  it("opens the diagnostics drawer over real signals only", () => {
    render(<RoastHeader {...BASE} />);
    expect(screen.queryByTestId("diagnostics-drawer")).toBeNull();
    fireEvent.click(screen.getByTestId("diagnostics-toggle"));
    const drawer = screen.getByTestId("diagnostics-drawer");
    expect(drawer).toHaveTextContent("development");
    expect(drawer).toHaveTextContent("running");
  });
});
