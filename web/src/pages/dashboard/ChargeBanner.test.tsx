import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { ChargeBanner } from "./ChargeBanner";

afterEach(cleanup);

const BAND = { minC: 170, maxC: 200 };

describe("ChargeBanner", () => {
  it("shows the in-window cue when preheating + bean in band, with live temp and the window range", () => {
    render(<ChargeBanner phase="preheating" beanTempC={178.4} chargeBand={BAND} />);
    const banner = screen.getByTestId("charge-banner");
    expect(banner).toHaveAttribute("data-state", "in_window");
    expect(banner).toHaveTextContent(/charge now/i);
    expect(banner).toHaveTextContent(/add beans/i);
    expect(banner).toHaveTextContent("178.4 °C");
    expect(banner).toHaveTextContent("170.0 °C");
    expect(banner).toHaveTextContent("200.0 °C");
  });

  it("shows the ESCALATED over-window warning above the band while still preheating (#211)", () => {
    render(<ChargeBanner phase="preheating" beanTempC={211.3} chargeBand={BAND} />);
    const banner = screen.getByTestId("charge-banner");
    expect(banner).toHaveAttribute("data-state", "over_window");
    // The cue must NOT disappear above the band — it escalates instead.
    expect(banner).toHaveTextContent(/over charge temperature/i);
    expect(banner).toHaveTextContent(/add beans now or reduce heat/i);
    // Still shows the live bean temp and the window range.
    expect(banner).toHaveTextContent("211.3 °C");
    expect(banner).toHaveTextContent("170.0 °C");
    expect(banner).toHaveTextContent("200.0 °C");
  });

  it("announces ONLY the CTA assertively — not the outer banner (announced once, not a dismissible toast)", () => {
    render(<ChargeBanner phase="preheating" beanTempC={185} chargeBand={BAND} />);
    const banner = screen.getByTestId("charge-banner");
    // The OUTER container is NOT a live region (#215 FIX G) — otherwise the ticking
    // figures it wraps would re-announce the whole alert every telemetry tick.
    expect(banner).not.toHaveAttribute("role", "alert");
    expect(banner).not.toHaveAttribute("aria-live");
    // The assertive region is the CTA heading alone.
    const cta = screen.getByTestId("charge-banner-cta");
    expect(cta).toHaveAttribute("role", "alert");
    expect(cta).toHaveTextContent(/charge now/i);
    // No dismiss control — it persists while the condition holds.
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("excludes the frequently-changing figures from the assertive region so they don't re-announce each tick (#215 FIX G)", () => {
    render(
      <ChargeBanner phase="preheating" beanTempC={185.7} chargeBand={BAND} dwellSeconds={95} />,
    );
    // The assertive element (the CTA, role=alert) must NOT contain the live bean
    // temp — otherwise a screen reader re-announces the alert every telemetry tick.
    const cta = screen.getByTestId("charge-banner-cta");
    expect(cta).toHaveAttribute("role", "alert");
    expect(cta).not.toHaveTextContent("185.7 °C");
    // The live bean-temp readout lives OUTSIDE the assertive subtree.
    const readout = screen.getByTestId("charge-banner-readout");
    expect(readout).toHaveTextContent("185.7 °C");
    expect(cta).not.toContainElement(readout);
    // The ticking dwell timer is aria-hidden (and also outside the CTA).
    const dwell = screen.getByTestId("charge-banner-dwell");
    expect(dwell).toHaveAttribute("aria-hidden", "true");
    expect(dwell).toHaveTextContent("01:35");
    expect(cta).not.toContainElement(dwell);
  });

  it("hides when preheating but the bean is below the band (not yet at charge)", () => {
    render(<ChargeBanner phase="preheating" beanTempC={120} chargeBand={BAND} />);
    expect(screen.queryByTestId("charge-banner")).toBeNull();
  });

  it("hides once the phase is no longer preheating (beans added → roasting), even over-band", () => {
    render(
      <ChargeBanner phase="roasting_pre_first_crack" beanTempC={215} chargeBand={BAND} />,
    );
    expect(screen.queryByTestId("charge-banner")).toBeNull();
  });

  it("hides before the profile band has hydrated", () => {
    render(<ChargeBanner phase="preheating" beanTempC={185} chargeBand={null} />);
    expect(screen.queryByTestId("charge-banner")).toBeNull();
  });

  it("shows the dwell timer when provided (over-preheat nudge)", () => {
    render(
      <ChargeBanner phase="preheating" beanTempC={185} chargeBand={BAND} dwellSeconds={95} />,
    );
    expect(screen.getByTestId("charge-banner-dwell")).toHaveTextContent("01:35");
  });

  it("omits the dwell line when no dwell is supplied", () => {
    render(<ChargeBanner phase="preheating" beanTempC={185} chargeBand={BAND} />);
    expect(screen.queryByTestId("charge-banner-dwell")).toBeNull();
  });
});
