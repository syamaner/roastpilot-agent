import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { MicStatus } from "@/lib/types";
import { MicStatusIcon } from "./MicStatusIcon";
import { resolveMicStatus } from "./micStatus";

afterEach(cleanup);

/** A capture-alive (OK/green) mic status — the replay-synthesized shape. */
const OK: MicStatus = {
  mic_health: "ok",
  audio_running: true,
  fc_status: "pending",
  queued_window_count: 3,
  emitted_window_count: 2,
  dropped_window_count: 0,
  processed_window_count: 2,
  reason: null,
  overflow_count_last_minute: 0,
  estimated_lost_audio_ms_last_minute: 0,
  total_overflow_count: 0,
};

describe("MicStatusIcon", () => {
  it("tints green for an OK mic and labels it", () => {
    render(<MicStatusIcon micStatus={OK} />);
    const icon = screen.getByTestId("mic-status");
    expect(icon).toHaveAttribute("data-health", "ok");
    expect(icon).toHaveTextContent(/mic ok/i);
  });

  it("tints red for an error mic (faulted detector) — never green", () => {
    render(
      <MicStatusIcon
        micStatus={{ ...OK, mic_health: "error", fc_status: "faulted", audio_running: false }}
      />,
    );
    const icon = screen.getByTestId("mic-status");
    expect(icon).toHaveAttribute("data-health", "error");
    expect(icon).toHaveTextContent(/mic error/i);
  });

  it("tints amber/idle for an idle mic (FC disabled)", () => {
    render(<MicStatusIcon micStatus={{ ...OK, mic_health: "idle", fc_status: "disabled" }} />);
    expect(screen.getByTestId("mic-status")).toHaveAttribute("data-health", "idle");
  });

  it("renders null mic_status as IDLE — a missing field is no info, NOT error/red", () => {
    render(<MicStatusIcon micStatus={null} />);
    const icon = screen.getByTestId("mic-status");
    // The invariant from the contract: null → idle (amber/grey), never error.
    expect(icon).toHaveAttribute("data-health", "idle");
    expect(icon).not.toHaveAttribute("data-health", "error");
  });

  it("treats undefined the same as null (idle, no info)", () => {
    render(<MicStatusIcon micStatus={undefined} />);
    expect(screen.getByTestId("mic-status")).toHaveAttribute("data-health", "idle");
  });

  it("exposes the health summary to assistive tech (aria-label + title)", () => {
    render(<MicStatusIcon micStatus={OK} />);
    const icon = screen.getByTestId("mic-status");
    expect(icon).toHaveAttribute("role", "status");
    expect(icon).toHaveAttribute("aria-label", expect.stringContaining("Mic OK"));
    expect(icon).toHaveAttribute("title", expect.stringContaining("Mic OK"));
    // Keyboard-focusable so the hover tooltip is also reachable via focus.
    expect(icon).toHaveAttribute("tabindex", "0");
  });

  it("tooltip carries audio running, FC status, the window counts, and reason", () => {
    render(
      <MicStatusIcon
        micStatus={{
          mic_health: "error",
          audio_running: false,
          fc_status: "unavailable",
          queued_window_count: 7,
          emitted_window_count: 5,
          dropped_window_count: 2,
          processed_window_count: 4,
          reason: "audio device not found",
          overflow_count_last_minute: 0,
          estimated_lost_audio_ms_last_minute: 0,
          total_overflow_count: 0,
        }}
      />,
    );
    const tooltip = within(screen.getByTestId("mic-status-tooltip"));
    expect(tooltip.getByText(/stopped/i)).toBeInTheDocument();
    expect(tooltip.getByText(/unavailable/i)).toBeInTheDocument();
    // The raw capture-alive counters behind the health.
    expect(tooltip.getByText("7")).toBeInTheDocument(); // queued
    expect(tooltip.getByText("5")).toBeInTheDocument(); // emitted
    expect(tooltip.getByText("4")).toBeInTheDocument(); // processed
    expect(tooltip.getByText("2")).toBeInTheDocument(); // dropped
    expect(tooltip.getByText(/audio device not found/i)).toBeInTheDocument();
  });

  it("#539: omits the overflow rows when total_overflow_count is 0 (a fresh/pre-0.1.13 session)", () => {
    render(<MicStatusIcon micStatus={OK} />);
    const tooltip = within(screen.getByTestId("mic-status-tooltip"));
    expect(tooltip.queryByText(/overflow/i)).toBeNull();
    expect(tooltip.queryByText(/lost audio/i)).toBeNull();
  });

  it("#539: shows the overflow diagnostics (MCP 0.1.13, coffee-roaster-mcp#190) when total_overflow_count > 0", () => {
    render(
      <MicStatusIcon
        micStatus={{
          ...OK,
          overflow_count_last_minute: 6,
          estimated_lost_audio_ms_last_minute: 128.4,
          total_overflow_count: 11,
        }}
      />,
    );
    const tooltip = within(screen.getByTestId("mic-status-tooltip"));
    expect(tooltip.getByText(/overflows \(1 min\)/i)).toBeInTheDocument();
    expect(tooltip.getByText("6")).toBeInTheDocument();
    expect(tooltip.getByText(/lost audio \(1 min\)/i)).toBeInTheDocument();
    expect(tooltip.getByText("128 ms")).toBeInTheDocument();
    expect(tooltip.getByText(/overflows \(total\)/i)).toBeInTheDocument();
    expect(tooltip.getByText("11")).toBeInTheDocument();
  });

  it("omits the reason row when there is no reason", () => {
    render(<MicStatusIcon micStatus={OK} />);
    const tooltip = within(screen.getByTestId("mic-status-tooltip"));
    // OK has reason: null — no stray reason text, only the labeled rows.
    expect(tooltip.queryByText(/null/i)).toBeNull();
  });

  it("maps fc_status 'pending' to the operator label 'listening'", () => {
    render(<MicStatusIcon micStatus={OK} />);
    const tooltip = within(screen.getByTestId("mic-status-tooltip"));
    expect(tooltip.getByText(/listening/i)).toBeInTheDocument();
  });
});

describe("resolveMicStatus (#200/Codex — live frame is authoritative)", () => {
  const SNAPSHOT: MicStatus = { ...OK, mic_health: "ok" };

  it("uses the run snapshot before any telemetry frame (hydrate paint)", () => {
    expect(resolveMicStatus(null, SNAPSHOT)).toBe(SNAPSHOT);
    expect(resolveMicStatus(undefined, SNAPSHOT)).toBe(SNAPSHOT);
  });

  it("uses the live frame's mic_status once a frame exists", () => {
    const live: MicStatus = { ...OK, mic_health: "error", fc_status: "faulted" };
    expect(resolveMicStatus({ mic_status: live }, SNAPSHOT)).toBe(live);
  });

  it("passes a live null THROUGH (idle) instead of the stale snapshot — the bug", () => {
    // A telemetry frame carrying mic_status: null is the documented idle case;
    // the icon must return to idle, NOT stick on the snapshot's green/red.
    expect(resolveMicStatus({ mic_status: null }, SNAPSHOT)).toBeNull();
  });

  it("returns null when neither source has a status", () => {
    expect(resolveMicStatus(null, null)).toBeNull();
    expect(resolveMicStatus(null, undefined)).toBeNull();
  });
});
