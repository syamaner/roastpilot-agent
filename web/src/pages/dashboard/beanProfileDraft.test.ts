import { describe, expect, it } from "vitest";

import {
  DEFAULT_BEAN_PROFILE_DRAFT,
  fieldEvidenceFor,
  fieldSourceFor,
  PROVENANCE_TRACKED_FIELDS,
  stripBidiControls,
  withFieldEdited,
  type BeanProfileDraft,
  type ProvenanceTrackedField,
} from "./beanProfileDraft";

/** A draft with the given `field_sources`/`field_evidence` maps layered on
 *  the default (the shape the draft-review UI reads from, #627). */
function draftWith(
  fieldSources: Record<string, string>,
  fieldEvidence: Record<string, string>,
): BeanProfileDraft {
  return {
    ...DEFAULT_BEAN_PROFILE_DRAFT,
    field_sources: fieldSources,
    field_evidence: fieldEvidence,
  };
}

describe("fieldSourceFor (#627)", () => {
  it("returns the provenance for each of the four tracked fields", () => {
    const draft = draftWith(
      {
        altitude_m: "origin_estimated",
        processing: "on_page",
        bean_species: "on_page",
        is_blend: "origin_estimated",
      },
      {},
    );
    expect(fieldSourceFor(draft, "altitude_m")).toBe("origin_estimated");
    expect(fieldSourceFor(draft, "processing")).toBe("on_page");
    expect(fieldSourceFor(draft, "bean_species")).toBe("on_page");
    expect(fieldSourceFor(draft, "is_blend")).toBe("origin_estimated");
  });

  it("returns undefined when the draft carries no field_sources at all", () => {
    expect(fieldSourceFor(DEFAULT_BEAN_PROFILE_DRAFT, "altitude_m")).toBeUndefined();
  });

  it("returns undefined for a tracked field absent from field_sources (unset, not scraped)", () => {
    const draft = draftWith({ processing: "on_page" }, {});
    expect(fieldSourceFor(draft, "altitude_m")).toBeUndefined();
  });

  it("returns undefined for an unrecognised field_sources value (defense-in-depth)", () => {
    const draft = draftWith({ altitude_m: "fabricated_value" }, {});
    expect(fieldSourceFor(draft, "altitude_m")).toBeUndefined();
  });

  it("ignores a field_sources entry keyed by a field this UI does not track", () => {
    // Cast past the type system the way a hostile/unexpected payload would
    // arrive over the wire — the whitelist must reject it at the KEY, not
    // rely on TypeScript to have already narrowed it.
    const draft = draftWith({ target_development_percent: "origin_estimated" }, {});
    expect(
      fieldSourceFor(draft, "target_development_percent" as ProvenanceTrackedField),
    ).toBeUndefined();
  });
});

describe("fieldEvidenceFor (#627)", () => {
  it("returns the captured quote for each of the four tracked fields", () => {
    const draft = draftWith(
      {},
      {
        altitude_m: "Grown at 1900-2100 masl",
        processing: "Fully washed and sun-dried",
        bean_species: "100% Arabica",
        is_blend: "This is a single-origin lot",
      },
    );
    expect(fieldEvidenceFor(draft, "altitude_m")).toBe("Grown at 1900-2100 masl");
    expect(fieldEvidenceFor(draft, "processing")).toBe("Fully washed and sun-dried");
    expect(fieldEvidenceFor(draft, "bean_species")).toBe("100% Arabica");
    expect(fieldEvidenceFor(draft, "is_blend")).toBe("This is a single-origin lot");
  });

  it("returns undefined when the draft carries no field_evidence at all", () => {
    expect(fieldEvidenceFor(DEFAULT_BEAN_PROFILE_DRAFT, "processing")).toBeUndefined();
  });

  it("returns undefined for a tracked field with no captured quote", () => {
    const draft = draftWith({}, { processing: "Washed" });
    expect(fieldEvidenceFor(draft, "bean_species")).toBeUndefined();
  });

  it("ignores a field_evidence entry keyed by a field this UI does not track", () => {
    const draft = draftWith({}, { name: "House Blend Extraordinaire" });
    expect(fieldEvidenceFor(draft, "name" as ProvenanceTrackedField)).toBeUndefined();
  });

  it("tracks exactly the four typed fields the story scopes (#627)", () => {
    expect([...PROVENANCE_TRACKED_FIELDS].sort()).toEqual(
      ["altitude_m", "bean_species", "is_blend", "processing"].sort(),
    );
  });
});

describe("withFieldEdited (#627 Codex round-2: clear-on-edit)", () => {
  it("applies the new value AND clears that field's provenance + evidence", () => {
    const draft = draftWith(
      { altitude_m: "origin_estimated", processing: "on_page" },
      { altitude_m: "Grown at 1900-2100 masl", processing: "Washed" },
    );
    const next = withFieldEdited(draft, "altitude_m", "1234");
    expect(next.altitude_m).toBe("1234");
    expect(next.field_sources).not.toHaveProperty("altitude_m");
    expect(next.field_evidence).not.toHaveProperty("altitude_m");
    // The sibling field's provenance/evidence is untouched.
    expect(next.field_sources?.processing).toBe("on_page");
    expect(next.field_evidence?.processing).toBe("Washed");
  });

  it("clears a free-text field's field_sources entry likewise (not just the four typed fields)", () => {
    const draft = draftWith({ bean_varietal: "origin_estimated" }, {});
    const next = withFieldEdited(draft, "bean_varietal", "Heirloom");
    expect(next.bean_varietal).toBe("Heirloom");
    expect(next.field_sources).not.toHaveProperty("bean_varietal");
  });

  it("clears is_blend's provenance/evidence via the boolean setter path", () => {
    const draft = draftWith(
      { is_blend: "origin_estimated" },
      { is_blend: "This is a single-origin lot" },
    );
    const next = withFieldEdited(draft, "is_blend", true);
    expect(next.is_blend).toBe(true);
    expect(next.field_sources).not.toHaveProperty("is_blend");
    expect(next.field_evidence).not.toHaveProperty("is_blend");
  });

  it("produces the SAME field_sources/field_evidence reference when the edited field carries none (no spurious churn)", () => {
    const draft = draftWith({ processing: "on_page" }, { processing: "Washed" });
    // Editing altitude_m, which has no provenance/evidence entry here.
    const next = withFieldEdited(draft, "altitude_m", "1500");
    expect(next.altitude_m).toBe("1500");
    expect(next.field_sources).toBe(draft.field_sources);
    expect(next.field_evidence).toBe(draft.field_evidence);
  });

  it("is a no-op on field_sources/field_evidence when the draft carries neither map at all", () => {
    const next = withFieldEdited(DEFAULT_BEAN_PROFILE_DRAFT, "name", "House blend");
    expect(next.name).toBe("House blend");
    expect(next.field_sources).toBeUndefined();
    expect(next.field_evidence).toBeUndefined();
  });
});

describe("stripBidiControls (#627 retro finding on #634)", () => {
  // Built from explicit codepoints, never literal characters in the test
  // source — embedding a real bidi control in a source file is its own
  // Trojan-Source-style footgun, in a test file as much as in production.
  const ALM = String.fromCodePoint(0x061c); // ARABIC LETTER MARK
  const LRM = String.fromCodePoint(0x200e); // LEFT-TO-RIGHT MARK
  const RLM = String.fromCodePoint(0x200f); // RIGHT-TO-LEFT MARK
  const LRE = String.fromCodePoint(0x202a); // LEFT-TO-RIGHT EMBEDDING
  const RLO = String.fromCodePoint(0x202e); // RIGHT-TO-LEFT OVERRIDE
  const PDF = String.fromCodePoint(0x202c); // POP DIRECTIONAL FORMATTING
  const LRI = String.fromCodePoint(0x2066); // LEFT-TO-RIGHT ISOLATE
  const FSI = String.fromCodePoint(0x2068); // FIRST STRONG ISOLATE
  const PDI = String.fromCodePoint(0x2069); // POP DIRECTIONAL ISOLATE

  it("strips a bidi-override control, leaving the surrounding text intact", () => {
    expect(stripBidiControls(`${RLO}reversed-looking${PDF} text`)).toBe("reversed-looking text");
  });

  it("strips an isolate pair", () => {
    expect(stripBidiControls(`${LRI}1900${PDI} masl`)).toBe("1900 masl");
    expect(stripBidiControls(`${FSI}auto-detected${PDI}`)).toBe("auto-detected");
  });

  it("strips embeddings + marks + the Arabic letter mark", () => {
    expect(stripBidiControls(`${LRE}embedded${PDF}`)).toBe("embedded");
    expect(stripBidiControls(`left${LRM}right${RLM}`)).toBe("leftright");
    expect(stripBidiControls(`${ALM}note`)).toBe("note");
  });

  it("strips every control at once, wherever it sits in the string", () => {
    const hostile = `${RLO}${LRI}${ALM}mixed${PDI}${PDF}${LRM}`;
    expect(stripBidiControls(hostile)).toBe("mixed");
  });

  it("passes plain ASCII text through unmodified", () => {
    const plain = "Washed and sun-dried on raised beds.";
    expect(stripBidiControls(plain)).toBe(plain);
  });

  it("passes legitimate RTL text (Arabic letters, no bidi controls) through unmodified", () => {
    const arabic = "هذه القهوة مغسولة ومجففة في الشمس";
    expect(stripBidiControls(arabic)).toBe(arabic);
  });

  it("is a no-op on an already-clean string with punctuation/diacritics", () => {
    const text = "Grown at 1,900–2,100 m — hand-picked & sun-dried.";
    expect(stripBidiControls(text)).toBe(text);
  });
});
