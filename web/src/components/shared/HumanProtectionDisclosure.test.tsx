import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { HumanProtectionProjection } from "@/lib/types";
import { HumanProtectionDisclosure } from "./HumanProtectionDisclosure";

const emptyProjection: HumanProtectionProjection = {
  human_endpoint: null,
  human_command_disposition: null,
  physical_intervention_required: null,
  emergency_fallback_disclosure: null,
};

const fallback = "Emergency fallback may stop the drum and close the chute. Admission or RPC return does not establish physical evacuation.";
const physicalSuccess = /cooling confirmed|\bevacuat(?:ed|ion confirmed)\b|beans dropped|\bempty\b/i;

function disclosure() {
  return within(screen.getByRole("region", { name: "Human-mode protection" }));
}

describe("HumanProtectionDisclosure state labels", () => {
  it.each([
    ["none", "No drop admitted"],
    ["admitted", "Drop admitted"],
    ["in_flight", "Drop command in progress"],
    ["issued", "Drop command issued"],
    ["unknown", "Drop outcome unknown"],
    ["withheld", "Drop withheld"],
    ["history_unknown", "Prior drop history unknown"],
  ] as const)("renders endpoint %s without a physical-success claim", (endpoint, label) => {
    render(<HumanProtectionDisclosure protection={{
      ...emptyProjection, human_endpoint: endpoint, emergency_fallback_disclosure: fallback,
    }} />);

    const state = disclosure().getByRole("definition");
    expect(state).toHaveTextContent(new RegExp(`^${label}$`));
    expect(state).not.toHaveTextContent(physicalSuccess);
    expect(disclosure().getByText(fallback)).toBeVisible();
  });

  it.each([
    ["settled", "Settled"],
    ["unknown", "Command outcome unknown"],
  ] as const)("renders command %s without a physical-success claim", (command, label) => {
    render(<HumanProtectionDisclosure protection={{
      ...emptyProjection, human_command_disposition: command,
    }} />);

    const state = disclosure().getByRole("definition");
    expect(state).toHaveTextContent(new RegExp(`^${label}$`));
    expect(state).not.toHaveTextContent(physicalSuccess);
  });

  it.each([
    [{ physical_intervention_required: true }, "Physical intervention required"],
    [{ emergency_fallback_disclosure: fallback }, fallback],
  ] as const)("shows supplied intervention or fallback without inventing a disposition", (fields, text) => {
    render(<HumanProtectionDisclosure protection={{ ...emptyProjection, ...fields }} />);

    const region = disclosure();
    expect(region.getByText(text)).toBeVisible();
    expect(region.queryByRole("term")).toBeNull();
    expect(region.queryByRole("definition")).toBeNull();
  });
});
