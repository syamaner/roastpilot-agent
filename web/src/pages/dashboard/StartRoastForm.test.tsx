import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api";
import type { RoastProfile } from "@/lib/types";
import { StartRoastForm } from "./StartRoastForm";

afterEach(cleanup);

/** Fill the two required bean fields + weight; defaults cover the rest. */
function fillMinimum() {
  fireEvent.change(screen.getByTestId("start-roast-name"), { target: { value: "Morning batch" } });
  fireEvent.change(screen.getByTestId("start-roast-bean_origin"), {
    target: { value: "Ethiopia Guji" },
  });
  fireEvent.change(screen.getByTestId("start-roast-bean_weight_grams"), {
    target: { value: "250" },
  });
}

describe("StartRoastForm", () => {
  it("renders with the profile defaults pre-filled", () => {
    render(<StartRoastForm onStart={vi.fn()} />);
    expect(screen.getByTestId("start-roast-charge_guidance_min_c")).toHaveValue(170);
    expect(screen.getByTestId("start-roast-charge_guidance_max_c")).toHaveValue(200);
    expect(screen.getByTestId("start-roast-initial_heat_percent")).toHaveValue(70);
    expect(screen.getByTestId("start-roast-initial_fan_percent")).toHaveValue(40);
    expect(screen.getByTestId("start-roast-target_drop_temp_c")).toHaveValue(205);
    expect(screen.getByTestId("start-roast-target_development_percent")).toHaveValue(20);
  });

  it("warns that starting commands real heat", () => {
    render(<StartRoastForm onStart={vi.fn()} />);
    expect(screen.getByTestId("start-roast-heat-note")).toHaveTextContent(/real heat/i);
  });

  it("submits the assembled profile with parsed numeric + null varietal", async () => {
    const onStart = vi.fn().mockResolvedValue(undefined);
    render(<StartRoastForm onStart={onStart} />);
    fillMinimum();
    fireEvent.submit(screen.getByTestId("start-roast-form"));

    await waitFor(() => expect(onStart).toHaveBeenCalledTimes(1));
    const profile = onStart.mock.calls[0][0] as RoastProfile;
    expect(profile).toEqual({
      name: "Morning batch",
      bean_origin: "Ethiopia Guji",
      bean_varietal: null,
      bean_weight_grams: 250,
      charge_guidance_min_c: 170,
      charge_guidance_max_c: 200,
      initial_heat_percent: 70,
      initial_fan_percent: 40,
      target_drop_temp_c: 205,
      target_development_percent: 20,
    });
  });

  it("passes an optional varietal through when provided", async () => {
    const onStart = vi.fn().mockResolvedValue(undefined);
    render(<StartRoastForm onStart={onStart} />);
    fillMinimum();
    fireEvent.change(screen.getByTestId("start-roast-bean_varietal"), {
      target: { value: "Heirloom" },
    });
    fireEvent.submit(screen.getByTestId("start-roast-form"));
    await waitFor(() => expect(onStart).toHaveBeenCalledTimes(1));
    expect((onStart.mock.calls[0][0] as RoastProfile).bean_varietal).toBe("Heirloom");
  });

  it("rejects missing required fields and does not call the API", () => {
    const onStart = vi.fn();
    render(<StartRoastForm onStart={onStart} />);
    fireEvent.submit(screen.getByTestId("start-roast-form"));
    expect(screen.getByTestId("start-roast-name-error")).toBeInTheDocument();
    expect(screen.getByTestId("start-roast-bean_origin-error")).toBeInTheDocument();
    expect(screen.getByTestId("start-roast-bean_weight_grams-error")).toBeInTheDocument();
    expect(onStart).not.toHaveBeenCalled();
  });

  it("rejects an out-of-range percent (heat > 100)", () => {
    const onStart = vi.fn();
    render(<StartRoastForm onStart={onStart} />);
    fillMinimum();
    fireEvent.change(screen.getByTestId("start-roast-initial_heat_percent"), {
      target: { value: "150" },
    });
    fireEvent.submit(screen.getByTestId("start-roast-form"));
    expect(screen.getByTestId("start-roast-initial_heat_percent-error")).toBeInTheDocument();
    expect(onStart).not.toHaveBeenCalled();
  });

  it("rejects a non-positive weight", () => {
    const onStart = vi.fn();
    render(<StartRoastForm onStart={onStart} />);
    fillMinimum();
    fireEvent.change(screen.getByTestId("start-roast-bean_weight_grams"), {
      target: { value: "0" },
    });
    fireEvent.submit(screen.getByTestId("start-roast-form"));
    expect(screen.getByTestId("start-roast-bean_weight_grams-error")).toBeInTheDocument();
    expect(onStart).not.toHaveBeenCalled();
  });

  it("rejects charge min >= max", () => {
    const onStart = vi.fn();
    render(<StartRoastForm onStart={onStart} />);
    fillMinimum();
    fireEvent.change(screen.getByTestId("start-roast-charge_guidance_min_c"), {
      target: { value: "210" },
    });
    fireEvent.submit(screen.getByTestId("start-roast-form"));
    expect(screen.getByTestId("start-roast-charge_guidance_max_c-error")).toHaveTextContent(
      /above min/i,
    );
    expect(onStart).not.toHaveBeenCalled();
  });

  it("rejects development percent at the 100 boundary (exclusive)", () => {
    const onStart = vi.fn();
    render(<StartRoastForm onStart={onStart} />);
    fillMinimum();
    fireEvent.change(screen.getByTestId("start-roast-target_development_percent"), {
      target: { value: "100" },
    });
    fireEvent.submit(screen.getByTestId("start-roast-form"));
    expect(screen.getByTestId("start-roast-target_development_percent-error")).toBeInTheDocument();
    expect(onStart).not.toHaveBeenCalled();
  });

  it("shows an inline 409 conflict error without crashing", async () => {
    const onStart = vi.fn().mockRejectedValue(new ApiError(409, "a roast is already active"));
    render(<StartRoastForm onStart={onStart} />);
    fillMinimum();
    fireEvent.submit(screen.getByTestId("start-roast-form"));
    await waitFor(() => expect(screen.getByTestId("start-roast-error")).toBeInTheDocument());
    expect(screen.getByTestId("start-roast-error")).toHaveTextContent(/already active/i);
    // The form is still interactive (button re-enabled) — no double-submit lock-up.
    expect(screen.getByTestId("start-roast-submit")).toBeEnabled();
  });

  it("surfaces a non-409 API error inline", async () => {
    const onStart = vi.fn().mockRejectedValue(new ApiError(500, "boom"));
    render(<StartRoastForm onStart={onStart} />);
    fillMinimum();
    fireEvent.submit(screen.getByTestId("start-roast-form"));
    await waitFor(() => expect(screen.getByTestId("start-roast-error")).toHaveTextContent("boom"));
  });

  it("disables the submit button while a submit is in flight and re-enables after", async () => {
    let resolve: (v: unknown) => void = () => {};
    const onStart = vi.fn().mockReturnValue(new Promise((r) => (resolve = r)));
    render(<StartRoastForm onStart={onStart} />);
    fillMinimum();
    fireEvent.submit(screen.getByTestId("start-roast-form"));
    await waitFor(() => expect(screen.getByTestId("start-roast-submit")).toBeDisabled());

    // A second submit while in flight must not call the API again.
    fireEvent.submit(screen.getByTestId("start-roast-form"));
    expect(onStart).toHaveBeenCalledTimes(1);

    resolve(undefined);
    await waitFor(() => expect(screen.getByTestId("start-roast-submit")).toBeEnabled());
  });
});
