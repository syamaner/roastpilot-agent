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

  it("bidi-isolates the quote text against a bidi-override character (security)", () => {
    // U+202E (RIGHT-TO-LEFT OVERRIDE) could otherwise visually invert this
    // quoted-authority framing ("page says: ...") without changing the
    // underlying (still-escaped) text.
    const hostile = "‮normal-looking text that renders reversed";
    renderFields({
      ...DEFAULT_BEAN_PROFILE_DRAFT,
      field_evidence: { processing: hostile },
    });
    const quoteText = screen.getByTestId(`${PREFIX}-processing-evidence-text`);
    expect(quoteText).toHaveAttribute("dir", "ltr");
    expect(quoteText).toHaveTextContent(hostile);
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
    // The clamp class is applied while collapsed — the whole point of the
    // toggle (#627 fold 1).
    expect(screen.getByTestId(`${PREFIX}-processing-evidence-text`)).toHaveClass(
      "line-clamp-2",
    );

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(toggle).toHaveTextContent(/less/i);
    // The full quote is present in the DOM even before expanding — the clamp
    // is visual-only, never a data loss.
    expect(screen.getByTestId(`${PREFIX}-processing-evidence`)).toHaveTextContent(longQuote);
  });

  it("does not clamp or show an expand toggle for a short quote (#627 fold 1)", () => {
    // Below the truncatable threshold (160 chars) but long enough to wrap
    // onto a couple of lines on a narrow (mobile single-column) layout — it
    // must render unclamped, since there is no toggle to un-clamp it with.
    const shortButWrapping =
      "Grown on a small family farm at moderate altitude, hand-picked and sun-dried on raised beds.";
    renderFields({
      ...DEFAULT_BEAN_PROFILE_DRAFT,
      field_evidence: { processing: shortButWrapping },
    });
    expect(screen.queryByTestId(`${PREFIX}-processing-evidence-toggle`)).toBeNull();
    expect(screen.getByTestId(`${PREFIX}-processing-evidence-text`)).not.toHaveClass(
      "line-clamp-2",
    );
  });
});

describe("BeanProfileFields draft-review aria-describedby association (#627b fold 2)", () => {
  it("input: aria-describedby is unchanged (hint only) when provenance/evidence are absent", () => {
    renderFields(DEFAULT_BEAN_PROFILE_DRAFT);
    const input = screen.getByTestId(`${PREFIX}-altitude_m`);
    expect(input).toHaveAttribute("aria-describedby", `${PREFIX}-altitude_m-hint`);
  });

  it("input: aria-describedby includes the provenance badge id, and the badge carries that id", () => {
    renderFields({
      ...DEFAULT_BEAN_PROFILE_DRAFT,
      field_sources: { altitude_m: "origin_estimated" },
    });
    const input = screen.getByTestId(`${PREFIX}-altitude_m`);
    const badge = screen.getByTestId("field-provenance-badge");
    expect(badge).toHaveAttribute("id", `${PREFIX}-altitude_m-provenance`);
    expect(input.getAttribute("aria-describedby")?.split(" ")).toContain(
      `${PREFIX}-altitude_m-provenance`,
    );
  });

  it("input: aria-describedby includes the evidence quote id, and the quote carries that id", () => {
    renderFields({
      ...DEFAULT_BEAN_PROFILE_DRAFT,
      field_evidence: { altitude_m: "Grown at 1900m" },
    });
    const input = screen.getByTestId(`${PREFIX}-altitude_m`);
    const quote = screen.getByTestId(`${PREFIX}-altitude_m-evidence`);
    expect(quote).toHaveAttribute("id", `${PREFIX}-altitude_m-evidence`);
    expect(input.getAttribute("aria-describedby")?.split(" ")).toContain(
      `${PREFIX}-altitude_m-evidence`,
    );
  });

  it("input: aria-describedby carries the hint, provenance, and evidence ids together, without clobbering the hint", () => {
    renderFields({
      ...DEFAULT_BEAN_PROFILE_DRAFT,
      field_sources: { altitude_m: "origin_estimated" },
      field_evidence: { altitude_m: "Grown at 1900m" },
    });
    const input = screen.getByTestId(`${PREFIX}-altitude_m`);
    const describedBy = input.getAttribute("aria-describedby")?.split(" ") ?? [];
    expect(describedBy).toEqual(
      expect.arrayContaining([
        `${PREFIX}-altitude_m-hint`,
        `${PREFIX}-altitude_m-provenance`,
        `${PREFIX}-altitude_m-evidence`,
      ]),
    );
  });

  it("select: aria-describedby carries the hint, provenance, and evidence ids together", () => {
    renderFields({
      ...DEFAULT_BEAN_PROFILE_DRAFT,
      field_sources: { processing: "on_page" },
      field_evidence: { processing: "Washed" },
    });
    const select = screen.getByTestId(`${PREFIX}-processing`);
    const describedBy = select.getAttribute("aria-describedby")?.split(" ") ?? [];
    expect(describedBy).toEqual(
      expect.arrayContaining([
        `${PREFIX}-processing-hint`,
        `${PREFIX}-processing-provenance`,
        `${PREFIX}-processing-evidence`,
      ]),
    );
  });

  it("checkbox: has no aria-describedby when provenance/evidence are absent (unchanged from today)", () => {
    renderFields(DEFAULT_BEAN_PROFILE_DRAFT);
    const checkbox = screen.getByTestId(`${PREFIX}-is_blend`);
    expect(checkbox).not.toHaveAttribute("aria-describedby");
  });

  it("checkbox: aria-describedby references the provenance badge + evidence quote when present", () => {
    renderFields({
      ...DEFAULT_BEAN_PROFILE_DRAFT,
      field_sources: { is_blend: "origin_estimated" },
      field_evidence: { is_blend: "A single-origin lot" },
    });
    const checkbox = screen.getByTestId(`${PREFIX}-is_blend`);
    const describedBy = checkbox.getAttribute("aria-describedby")?.split(" ") ?? [];
    expect(describedBy).toEqual(
      expect.arrayContaining([`${PREFIX}-is_blend-provenance`, `${PREFIX}-is_blend-evidence`]),
    );
  });
});
