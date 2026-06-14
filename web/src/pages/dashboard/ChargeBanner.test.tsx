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

  it("is an assertive alert (announced, not a dismissible toast)", () => {
    render(<ChargeBanner phase="preheating" beanTempC={185} chargeBand={BAND} />);
    const banner = screen.getByTestId("charge-banner");
    expect(banner).toHaveAttribute("role", "alert");
    expect(banner).toHaveAttribute("aria-live", "assertive");
    // No dismiss control — it persists while the condition holds.
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("keeps the ticking dwell timer OUT of the assertive region (aria-hidden) so it is not re-announced each second (#215)", () => {
    render(
      <ChargeBanner phase="preheating" beanTempC={185} chargeBand={BAND} dwellSeconds={95} />,
    );
    const banner = screen.getByTestId("charge-banner");
    // The banner itself stays assertive (CTA/heading announce once).
    expect(banner).toHaveAttribute("aria-live", "assertive");
    const dwell = screen.getByTestId("charge-banner-dwell");
    expect(dwell).toHaveAttribute("aria-hidden", "true");
    expect(dwell).toHaveTextContent("01:35");
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
