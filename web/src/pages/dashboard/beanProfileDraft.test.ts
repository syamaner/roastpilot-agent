import { describe, expect, it } from "vitest";

import {
  DEFAULT_BEAN_PROFILE_DRAFT,
  fieldEvidenceFor,
  fieldSourceFor,
  PROVENANCE_TRACKED_FIELDS,
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
