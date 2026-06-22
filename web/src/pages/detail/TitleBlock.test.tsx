import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { RoastDetail } from "@/lib/types";
import { FIXTURE_DETAIL } from "./fixture";
import { TitleBlock } from "./TitleBlock";
import type { HeadlineStats } from "./traceModel";

afterEach(cleanup);

const STATS: HeadlineStats = {
  totalSeconds: 600,
  firstCrackSeconds: 480,
  firstCrackTempC: 196,
  dropSeconds: 600,
  dropTempC: 218,
  developmentPercent: 20,
};

/** Build a detail with an overridden profile (#164 identity render tests). */
function detailWith(profile: Partial<RoastDetail["profile"]>): RoastDetail {
  return { ...FIXTURE_DETAIL, profile: { ...FIXTURE_DETAIL.profile, ...profile } };
}

describe("TitleBlock bean identity (#164)", () => {
  it("renders country · farm, species tag, and the description from the profile", () => {
    render(
      <TitleBlock
        detail={detailWith({
          country: "Ethiopia",
          farm: "Gedeb — Worka Sakaro",
          bean_species: "arabica",
          is_blend: false,
          description: "Washed; jasmine, bergamot.",
        })}
        stats={STATS}
      />,
    );
    expect(screen.getByTestId("bean-provenance")).toHaveTextContent("Ethiopia · Gedeb — Worka Sakaro");
    expect(screen.getByTestId("bean-tag-arabica")).toHaveTextContent(/arabica/i);
    expect(screen.queryByTestId("bean-tag-blend")).not.toBeInTheDocument();
    expect(screen.getByTestId("bean-description")).toHaveTextContent("Washed; jasmine, bergamot.");
  });

  it("shows the Blend tag when the profile is a blend", () => {
    render(
      <TitleBlock detail={detailWith({ is_blend: true })} stats={STATS} />,
    );
    expect(screen.getByTestId("bean-tag-blend")).toHaveTextContent(/blend/i);
  });

  it("renders the source URL as a new-tab link with the right href (#315)", () => {
    render(
      <TitleBlock
        detail={detailWith({
          source_url: "https://redber.co.uk/products/ethiopia-yirgacheffe-koke",
        })}
        stats={STATS}
      />,
    );
    const link = screen.getByTestId("bean-source-url");
    expect(link).toHaveAttribute(
      "href",
      "https://redber.co.uk/products/ethiopia-yirgacheffe-koke",
    );
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("renders no source-URL link when absent (#315 back-compat, no broken anchor)", () => {
    render(<TitleBlock detail={detailWith({ source_url: null })} stats={STATS} />);
    expect(screen.queryByTestId("bean-source-url")).not.toBeInTheDocument();
  });

  it("omits the identity rows for a pre-#164 profile (back-compat)", () => {
    render(
      <TitleBlock
        detail={detailWith({
          country: null,
          farm: null,
          bean_species: null,
          is_blend: false,
          description: null,
        })}
        stats={STATS}
      />,
    );
    expect(screen.queryByTestId("bean-provenance")).not.toBeInTheDocument();
    expect(screen.queryByTestId("bean-tags")).not.toBeInTheDocument();
    expect(screen.queryByTestId("bean-description")).not.toBeInTheDocument();
    // The original origin line is unaffected.
    expect(screen.getByTestId("bean-origin")).toBeInTheDocument();
  });
});
