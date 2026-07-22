import { useState } from "react";

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

  // Built from explicit codepoints rather than literal characters in the test
  // source — same reasoning as production: a raw bidi control embedded in a
  // source file is its own Trojan-Source-style footgun.
  const RLO = String.fromCodePoint(0x202e); // RIGHT-TO-LEFT OVERRIDE
  const PDF = String.fromCodePoint(0x202c); // POP DIRECTIONAL FORMATTING
  const LRI = String.fromCodePoint(0x2066); // LEFT-TO-RIGHT ISOLATE
  const PDI = String.fromCodePoint(0x2069); // POP DIRECTIONAL ISOLATE

  it("keeps the ltr/isolate attributes as defence-in-depth even though the control itself is stripped", () => {
    renderFields({
      ...DEFAULT_BEAN_PROFILE_DRAFT,
      field_evidence: { processing: `${RLO}normal-looking text that renders reversed` },
    });
    const quoteText = screen.getByTestId(`${PREFIX}-processing-evidence-text`);
    expect(quoteText).toHaveAttribute("dir", "ltr");
  });

  it("strips a bidi-override control from the rendered quote, leaving the rest of the text intact (retro finding on #634)", () => {
    // Not just isolation: `dir`/unicode-bidi only shield the SURROUNDING
    // layout — a control INSIDE the quote still reorders the quote's own
    // rendered characters, so it must be stripped, not merely isolated.
    renderFields({
      ...DEFAULT_BEAN_PROFILE_DRAFT,
      field_evidence: { processing: `${RLO}normal-looking text that renders reversed${PDF}` },
    });
    const quoteText = screen.getByTestId(`${PREFIX}-processing-evidence-text`);
    expect(quoteText.textContent).not.toContain(RLO);
    expect(quoteText.textContent).not.toContain(PDF);
    expect(quoteText).toHaveTextContent("normal-looking text that renders reversed");
  });

  it("strips a bidi-isolate pair from the rendered quote", () => {
    renderFields({
      ...DEFAULT_BEAN_PROFILE_DRAFT,
      field_evidence: { altitude_m: `${LRI}1900${PDI} masl` },
    });
    const quoteText = screen.getByTestId(`${PREFIX}-altitude_m-evidence-text`);
    expect(quoteText.textContent).not.toContain(LRI);
    expect(quoteText.textContent).not.toContain(PDI);
    expect(quoteText).toHaveTextContent("1900 masl");
  });

  it("passes legitimate RTL text (Arabic letters, no bidi controls) through unmodified", () => {
    const arabic = "هذه القهوة مغسولة ومجففة في الشمس";
    renderFields({
      ...DEFAULT_BEAN_PROFILE_DRAFT,
      field_evidence: { bean_species: arabic },
    });
    const quoteText = screen.getByTestId(`${PREFIX}-bean_species-evidence-text`);
    expect(quoteText).toHaveTextContent(arabic);
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

describe("BeanProfileFields draft-review sr-only warning + contextual toggle names (#627b Codex round-3)", () => {
  it("the 'review' badge carries a full sr-only warning, referenced via aria-describedby, distinct from the short visible text", () => {
    renderFields({
      ...DEFAULT_BEAN_PROFILE_DRAFT,
      field_sources: { altitude_m: "origin_estimated" },
    });
    const badge = screen.getByTestId("field-provenance-badge");
    // The referenced badge element (via aria-describedby on the input) is the
    // one carrying the full warning — assert it lives WITHIN that element.
    const srOnly = badge.querySelector(".sr-only");
    expect(srOnly).not.toBeNull();
    expect(srOnly).toHaveTextContent(/not confirmed on the vendor page.*review before use/i);
    // The short visible fragment is hidden from the accessibility tree so the
    // description isn't announced twice (fragment + full sentence).
    const visible = badge.querySelector('[aria-hidden="true"]');
    expect(visible).toHaveTextContent(/review/i);
  });

  it("the 'on page' badge also carries a full sr-only description", () => {
    renderFields({
      ...DEFAULT_BEAN_PROFILE_DRAFT,
      field_sources: { bean_species: "on_page" },
    });
    const badge = screen.getByTestId("field-provenance-badge");
    const srOnly = badge.querySelector(".sr-only");
    expect(srOnly).not.toBeNull();
    expect(srOnly).toHaveTextContent(/confirmed on the vendor page/i);
  });

  it("gives two rendered quotes' expand toggles distinct, field-naming accessible names", () => {
    const longAltitude = "Grown at high altitude in volcanic soil. ".repeat(6).trim();
    const longProcessing = "Fully washed then dried slowly on raised African beds. ".repeat(4).trim();
    renderFields({
      ...DEFAULT_BEAN_PROFILE_DRAFT,
      field_evidence: { altitude_m: longAltitude, processing: longProcessing },
    });
    const altitudeToggle = screen.getByTestId(`${PREFIX}-altitude_m-evidence-toggle`);
    const processingToggle = screen.getByTestId(`${PREFIX}-processing-evidence-toggle`);
    const altitudeName = altitudeToggle.getAttribute("aria-label");
    const processingName = processingToggle.getAttribute("aria-label");
    expect(altitudeName).toEqual(expect.stringContaining("Altitude"));
    expect(processingName).toEqual(expect.stringContaining("Processing"));
    expect(altitudeName).not.toEqual(processingName);
  });

  it("the toggle's aria-controls points at the quote block's id, and aria-expanded still toggles", () => {
    const longQuote = "Grown at high altitude in volcanic soil. ".repeat(6).trim();
    renderFields({
      ...DEFAULT_BEAN_PROFILE_DRAFT,
      field_evidence: { processing: longQuote },
    });
    const toggle = screen.getByTestId(`${PREFIX}-processing-evidence-toggle`);
    const quote = screen.getByTestId(`${PREFIX}-processing-evidence`);
    expect(toggle).toHaveAttribute("aria-controls", quote.id);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).toHaveAttribute("aria-label", expect.stringMatching(/^Show/));

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(toggle).toHaveAttribute("aria-label", expect.stringMatching(/^Hide/));
  });

  it("names the blend toggle's quote expand button 'Blend'", () => {
    const longQuote = "This lot is single-origin, not a blend of multiple farms or lots. ".repeat(3).trim();
    renderFields({
      ...DEFAULT_BEAN_PROFILE_DRAFT,
      field_evidence: { is_blend: longQuote },
    });
    const toggle = screen.getByTestId(`${PREFIX}-is_blend-evidence-toggle`);
    expect(toggle.getAttribute("aria-label")).toEqual(expect.stringContaining("Blend"));
  });
});

describe("BeanProfileFields is_blend tri-state (#637, #654 fold 2)", () => {
  it("renders no unresolved cue for the common case (is_blend_unresolved absent)", () => {
    renderFields(DEFAULT_BEAN_PROFILE_DRAFT);
    expect(screen.queryByTestId(`${PREFIX}-is_blend-unresolved`)).toBeNull();
  });

  it("shows an unresolved cue + caution hint when is_blend_unresolved is true", () => {
    renderFields({ ...DEFAULT_BEAN_PROFILE_DRAFT, is_blend_unresolved: true });
    const badge = screen.getByTestId(`${PREFIX}-is_blend-unresolved`);
    expect(badge).toHaveTextContent(/choose/i);
    expect(screen.getByText(/didn't say/i)).toBeInTheDocument();
  });

  it("replaces the plain checkbox with an explicit Single origin / Blend choice while unresolved (#654 round 2 fold 2)", () => {
    renderFields({ ...DEFAULT_BEAN_PROFILE_DRAFT, is_blend_unresolved: true });
    // No plain checkbox — a checkbox's first interaction can only ever SET it,
    // so confirming the safe default (single origin) would be undiscoverable.
    expect(screen.queryByTestId(`${PREFIX}-is_blend`)).toBeNull();
    expect(screen.getByTestId(`${PREFIX}-is_blend-choose-single-origin`)).toBeInTheDocument();
    expect(screen.getByTestId(`${PREFIX}-is_blend-choose-blend`)).toBeInTheDocument();
  });

  it("choosing 'Single origin' resolves is_blend to false and the plain checkbox returns", () => {
    const onBlendChange = vi.fn();
    render(
      <BeanProfileFields
        draft={{ ...DEFAULT_BEAN_PROFILE_DRAFT, is_blend_unresolved: true }}
        errors={{}}
        onChange={vi.fn()}
        onBlendChange={onBlendChange}
        testIdPrefix={PREFIX}
        showDefaultWeight
      />,
    );
    fireEvent.click(screen.getByTestId(`${PREFIX}-is_blend-choose-single-origin`));
    expect(onBlendChange).toHaveBeenCalledWith(false);
  });

  it("choosing 'Blend' resolves is_blend to true", () => {
    const onBlendChange = vi.fn();
    render(
      <BeanProfileFields
        draft={{ ...DEFAULT_BEAN_PROFILE_DRAFT, is_blend_unresolved: true }}
        errors={{}}
        onChange={vi.fn()}
        onBlendChange={onBlendChange}
        testIdPrefix={PREFIX}
        showDefaultWeight
      />,
    );
    fireEvent.click(screen.getByTestId(`${PREFIX}-is_blend-choose-blend`));
    expect(onBlendChange).toHaveBeenCalledWith(true);
  });

  it("renders the plain checkbox (not the two-choice control) once resolved", () => {
    renderFields({ ...DEFAULT_BEAN_PROFILE_DRAFT, is_blend: true });
    expect(screen.getByTestId(`${PREFIX}-is_blend`)).toBeChecked();
    expect(screen.queryByTestId(`${PREFIX}-is_blend-choose-single-origin`)).toBeNull();
    expect(screen.queryByTestId(`${PREFIX}-is_blend-choose-blend`)).toBeNull();
  });

  it("renders the validation error (post-submit-attempt) in place of the hint, not alongside it", () => {
    render(
      <BeanProfileFields
        draft={{ ...DEFAULT_BEAN_PROFILE_DRAFT, is_blend_unresolved: true }}
        errors={{ is_blend: "The vendor page didn't say — choose before saving." }}
        onChange={vi.fn()}
        onBlendChange={vi.fn()}
        testIdPrefix={PREFIX}
        showDefaultWeight
      />,
    );
    expect(screen.getByTestId(`${PREFIX}-is_blend-error`)).toHaveTextContent(/didn't say/i);
    // The unresolved BADGE (next to the label) is independent of the error slot
    // and still renders — only the hint/error TEXT slot below the checkbox
    // switches from the caution hint to the fault-coloured error.
    expect(screen.getByTestId(`${PREFIX}-is_blend-unresolved`)).toBeInTheDocument();
  });
});

describe("BeanProfileFields is_blend resolution — a11y (#658 round 1)", () => {
  /** A minimal STATEFUL parent mimicking a real consumer: owns draft + errors
   *  and updates them on `onBlendChange`, so the unresolved→resolved
   *  TRANSITION genuinely happens across renders — a fixed prop snapshot
   *  (the pattern the rest of this file uses) can't exercise either fold,
   *  since both are about what happens WHEN the field resolves, not a
   *  single static state. Deliberately does NOT clear `errors.is_blend`
   *  itself on resolve (unlike `BeanProfileModal`'s `onBlendChange`) — fold
   *  1 is exactly the claim that the structural gate hides the error even
   *  when the parent doesn't. */
  function StatefulWrapper({
    initialErrors = {},
  }: {
    initialErrors?: Record<string, string>;
  }): React.JSX.Element {
    const [draft, setDraft] = useState<BeanProfileDraft>({
      ...DEFAULT_BEAN_PROFILE_DRAFT,
      is_blend_unresolved: true,
    });
    const [errors] = useState(initialErrors);
    return (
      <BeanProfileFields
        draft={draft}
        errors={errors}
        onChange={vi.fn()}
        onBlendChange={(checked) =>
          setDraft((d) => ({ ...d, is_blend: checked, is_blend_unresolved: undefined }))
        }
        testIdPrefix={PREFIX}
        showDefaultWeight
      />
    );
  }

  it("hides the is_blend error the moment the field resolves, even when the parent never clears it (fold 1)", () => {
    render(
      <StatefulWrapper
        initialErrors={{ is_blend: "The vendor page didn't say — choose before saving." }}
      />,
    );
    expect(screen.getByTestId(`${PREFIX}-is_blend-error`)).toBeInTheDocument();

    fireEvent.click(screen.getByTestId(`${PREFIX}-is_blend-choose-single-origin`));

    // Gone from the DOM entirely (not merely re-styled) — the wrapper above
    // never touched `errors`, so this is the structural gate at work, not a
    // parent clearing it.
    expect(screen.queryByTestId(`${PREFIX}-is_blend-error`)).toBeNull();
    const checkbox = screen.getByTestId(`${PREFIX}-is_blend`);
    expect(checkbox.getAttribute("aria-describedby") ?? "").not.toContain("is_blend-error");
  });

  it("moves focus to the restored checkbox once resolved (fold 2)", () => {
    render(<StatefulWrapper />);
    screen.getByTestId(`${PREFIX}-is_blend-choose-single-origin`).focus();

    fireEvent.click(screen.getByTestId(`${PREFIX}-is_blend-choose-single-origin`));

    expect(document.activeElement).toBe(screen.getByTestId(`${PREFIX}-is_blend`));
  });
});
