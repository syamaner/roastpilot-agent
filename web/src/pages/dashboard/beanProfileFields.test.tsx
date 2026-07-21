import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { BeanProfileFields } from "./beanProfileFields";
import { DEFAULT_BEAN_PROFILE_DRAFT, type BeanProfileDraft } from "./beanProfileDraft";

afterEach(cleanup);

const PREFIX = "test";

function renderFields(draft: BeanProfileDraft) {
  render(
    <BeanProfileFields
      draft={draft}
      errors={{}}
      onChange={vi.fn()}
      onBlendChange={vi.fn()}
      testIdPrefix={PREFIX}
      showDefaultWeight
    />,
  );
}

describe("BeanProfileFields draft-review provenance + evidence (#627b)", () => {
  it("renders zero provenance/evidence noise when field_sources/field_evidence are absent (the common case)", () => {
    renderFields(DEFAULT_BEAN_PROFILE_DRAFT);
    expect(screen.queryAllByTestId("field-provenance-badge")).toHaveLength(0);
    for (const field of ["bean_species", "processing", "altitude_m", "is_blend"]) {
      expect(screen.queryByTestId(`${PREFIX}-${field}-evidence`)).toBeNull();
    }
  });

  it("shows an 'on page' badge for a code-confirmed typed field", () => {
    renderFields({
      ...DEFAULT_BEAN_PROFILE_DRAFT,
      field_sources: { bean_species: "on_page" },
    });
    const badges = screen.getAllByTestId("field-provenance-badge");
    expect(badges).toHaveLength(1);
    expect(badges[0]).toHaveAttribute("data-provenance", "on_page");
    expect(badges[0]).toHaveTextContent(/on page/i);
  });

  it("shows a 'review' badge for an origin-estimated typed field", () => {
    renderFields({
      ...DEFAULT_BEAN_PROFILE_DRAFT,
      field_sources: { altitude_m: "origin_estimated" },
    });
    const badges = screen.getAllByTestId("field-provenance-badge");
    expect(badges).toHaveLength(1);
    expect(badges[0]).toHaveAttribute("data-provenance", "origin_estimated");
    expect(badges[0]).toHaveTextContent(/review/i);
    // The full "not confirmed" framing is available (a11y title), not just the
    // short badge word.
    expect(badges[0]).toHaveAttribute("title", expect.stringMatching(/not confirmed/i));
  });

  it("renders provenance badges for all four typed fields independently, including the blend toggle", () => {
    renderFields({
      ...DEFAULT_BEAN_PROFILE_DRAFT,
      field_sources: {
        bean_species: "on_page",
        processing: "origin_estimated",
        altitude_m: "on_page",
        is_blend: "origin_estimated",
      },
    });
    expect(screen.getAllByTestId("field-provenance-badge")).toHaveLength(4);
  });

  it("renders the captured vendor quote as visibly-quoted, escaped text — never HTML", () => {
    const hostile = "<script>alert(1)</script>";
    const { container } = render(
      <BeanProfileFields
        draft={{
          ...DEFAULT_BEAN_PROFILE_DRAFT,
          field_evidence: { processing: hostile },
        }}
        errors={{}}
        onChange={vi.fn()}
        onBlendChange={vi.fn()}
        testIdPrefix={PREFIX}
        showDefaultWeight
      />,
    );
    const quote = screen.getByTestId(`${PREFIX}-processing-evidence`);
    // The literal, unexecuted string is present as TEXT...
    expect(quote).toHaveTextContent(hostile);
    // ...never parsed into a real <script> element anywhere in the tree.
    expect(container.querySelector("script")).toBeNull();
    expect(container.innerHTML).not.toContain("<script>alert(1)</script>");
  });

  it("renders the quote for each of the four typed fields under its own field", () => {
    renderFields({
      ...DEFAULT_BEAN_PROFILE_DRAFT,
      field_evidence: {
        bean_species: "100% Arabica",
        processing: "Washed and sun-dried on raised beds",
        altitude_m: "Grown between 1900 and 2100 masl",
        is_blend: "A single origin lot, not a blend",
      },
    });
    expect(screen.getByTestId(`${PREFIX}-bean_species-evidence`)).toHaveTextContent(
      "100% Arabica",
    );
    expect(screen.getByTestId(`${PREFIX}-processing-evidence`)).toHaveTextContent(
      "Washed and sun-dried on raised beds",
    );
    expect(screen.getByTestId(`${PREFIX}-altitude_m-evidence`)).toHaveTextContent(
      "Grown between 1900 and 2100 masl",
    );
    expect(screen.getByTestId(`${PREFIX}-is_blend-evidence`)).toHaveTextContent(
      "A single origin lot, not a blend",
    );
  });

  it("ignores an evidence/source entry keyed by a field this UI does not track", () => {
    renderFields({
      ...DEFAULT_BEAN_PROFILE_DRAFT,
      field_sources: { name: "on_page" },
      field_evidence: { name: "House Blend Extraordinaire" },
    });
    expect(screen.queryAllByTestId("field-provenance-badge")).toHaveLength(0);
    expect(screen.queryByTestId(`${PREFIX}-name-evidence`)).toBeNull();
  });

  it("clamps a long quote behind an accessible expand toggle", () => {
    const longQuote = "Grown at high altitude in volcanic soil. ".repeat(6).trim();
    renderFields({
      ...DEFAULT_BEAN_PROFILE_DRAFT,
      field_evidence: { processing: longQuote },
    });
    const toggle = screen.getByTestId(`${PREFIX}-processing-evidence-toggle`);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).toHaveTextContent(/more/i);

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(toggle).toHaveTextContent(/less/i);
    // The full quote is present in the DOM even before expanding — the clamp
    // is visual-only, never a data loss.
    expect(screen.getByTestId(`${PREFIX}-processing-evidence`)).toHaveTextContent(longQuote);
  });

  it("does not show an expand toggle for a short quote", () => {
    renderFields({
      ...DEFAULT_BEAN_PROFILE_DRAFT,
      field_evidence: { processing: "Washed" },
    });
    expect(screen.queryByTestId(`${PREFIX}-processing-evidence-toggle`)).toBeNull();
  });
});
