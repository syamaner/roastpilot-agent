import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api, ApiError } from "@/lib/api";
import type { BeanProfile, BeanProfileDraftResponse, BeanProfileInput } from "@/lib/types";
import { BeanProfileModal } from "./BeanProfileModal";
import * as beanProfileDraft from "./beanProfileDraft";
import { FIXTURE_DRAFT_RESPONSE, FIXTURE_KOKE } from "./beanProfileFixture";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

/** A saved BeanProfile echoed back by the mocked save (the server response). */
function savedFrom(input: BeanProfileInput): BeanProfile {
  return { ...input, id: "new-id", created_at: "t", updated_at: "t" };
}

/** A promise plus its own `resolve`/`reject`, for tests that control exactly
 *  when an in-flight `api.draftBeanFromUrl` call settles (#654 fold 4). */
function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (err: unknown) => void;
} {
  let resolve!: (value: T) => void;
  let reject!: (err: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/**
 * Dispatches a button click and an Enter keydown on the draft URL input
 * inside the SAME `act()` batch (#654 fold 4, round 2 fold 1). Two SEPARATE
 * `fireEvent` calls would not reproduce a genuine race — each is its own
 * flush, so `drafting` (React state) would already have committed `true`
 * before the second fired, and the `drafting`-based guard alone would
 * correctly block it before ever calling the API a second time (verified
 * empirically while building this helper). Batching both dispatches into one
 * `act()` call defers that flush until after BOTH handlers have already run
 * against the SAME pre-update closure — the same "genuinely overlapping"
 * edge a very fast double-fire (a rapid double-click, or a stray Enter
 * landing the same tick as a click) could hit in a real browser before React
 * gets a chance to repaint the disabled button. The synchronous `draftingRef`
 * guard (round 2 fold 1) closes this specific window: even with BOTH
 * handlers reading the same stale closure, only ONE ever calls the API,
 * since the ref (unlike state) is read/written live within that same batch.
 */
function fireOverlappingDraftRequests(button: HTMLElement, input: HTMLElement): void {
  act(() => {
    button.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    input.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }),
    );
  });
}

describe("BeanProfileModal add mode (#303)", () => {
  it("POSTs the captured input and reports the saved profile", async () => {
    const onSave = vi.fn(async (input: BeanProfileInput) => savedFrom(input));
    const onSaved = vi.fn();
    render(
      <BeanProfileModal mode="add" onSave={onSave} onSaved={onSaved} onClose={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("bean-profile-name"), {
      target: { value: "House blend" },
    });
    fireEvent.change(screen.getByTestId("bean-profile-bean_origin"), {
      target: { value: "Brazil" },
    });
    fireEvent.submit(screen.getByTestId("bean-profile-form"));

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    const input = onSave.mock.calls[0][0];
    expect(input.name).toBe("House blend");
    expect(input.bean_origin).toBe("Brazil");
    // The modal owns the default charge weight (pre-filled 250).
    expect(input.default_bean_weight_grams).toBe(250);
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
  });

  it("captures the product URL on the saved profile (#315)", async () => {
    const onSave = vi.fn(async (input: BeanProfileInput) => savedFrom(input));
    render(
      <BeanProfileModal mode="add" onSave={onSave} onSaved={vi.fn()} onClose={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("bean-profile-name"), { target: { value: "Kenya" } });
    fireEvent.change(screen.getByTestId("bean-profile-bean_origin"), {
      target: { value: "Kenya" },
    });
    fireEvent.change(screen.getByTestId("bean-profile-source_url"), {
      target: { value: "https://roaster.example.com/kenya-aa" },
    });
    fireEvent.submit(screen.getByTestId("bean-profile-form"));
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave.mock.calls[0][0].source_url).toBe("https://roaster.example.com/kenya-aa");
  });

  it("blocks save and shows field errors when required fields are blank", () => {
    const onSave = vi.fn();
    render(
      <BeanProfileModal mode="add" onSave={onSave} onSaved={vi.fn()} onClose={vi.fn()} />,
    );
    fireEvent.submit(screen.getByTestId("bean-profile-form"));
    expect(screen.getByTestId("bean-profile-name-error")).toBeInTheDocument();
    expect(screen.getByTestId("bean-profile-bean_origin-error")).toBeInTheDocument();
    expect(onSave).not.toHaveBeenCalled();
  });

  it("surfaces a 422 from the server inline", async () => {
    const onSave = vi.fn().mockRejectedValue(new ApiError(422, "invalid charge band"));
    render(
      <BeanProfileModal mode="add" onSave={onSave} onSaved={vi.fn()} onClose={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("bean-profile-name"), { target: { value: "X" } });
    fireEvent.change(screen.getByTestId("bean-profile-bean_origin"), {
      target: { value: "Y" },
    });
    fireEvent.submit(screen.getByTestId("bean-profile-form"));
    await waitFor(() =>
      expect(screen.getByTestId("bean-profile-error")).toHaveTextContent(/invalid charge band/i),
    );
  });

  it("does not offer Archive in add mode", () => {
    render(
      <BeanProfileModal
        mode="add"
        onSave={vi.fn()}
        onSaved={vi.fn()}
        onClose={vi.fn()}
        onArchive={vi.fn()}
      />,
    );
    expect(screen.queryByTestId("bean-profile-archive")).toBeNull();
  });
});

describe("BeanProfileModal edit mode (#303)", () => {
  it("pre-fills from the profile and PUTs the edited input", async () => {
    const onSave = vi.fn(async (input: BeanProfileInput) => savedFrom(input));
    render(
      <BeanProfileModal
        mode="edit"
        profile={FIXTURE_KOKE}
        onSave={onSave}
        onSaved={vi.fn()}
        onClose={vi.fn()}
      />,
    );
    // Pre-filled from the selected profile.
    expect(screen.getByTestId("bean-profile-name")).toHaveValue(FIXTURE_KOKE.name);
    expect(screen.getByTestId("bean-profile-default_bean_weight_grams")).toHaveValue(250);
    // Edit a field and save.
    fireEvent.change(screen.getByTestId("bean-profile-target_drop_temp_c"), {
      target: { value: "192" },
    });
    fireEvent.submit(screen.getByTestId("bean-profile-form"));
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave.mock.calls[0][0].target_drop_temp_c).toBe(192);
  });

  it("shows the future-roasts-only note in edit mode", () => {
    render(
      <BeanProfileModal
        mode="edit"
        profile={FIXTURE_KOKE}
        onSave={vi.fn()}
        onSaved={vi.fn()}
        onClose={vi.fn()}
      />,
    );
    expect(screen.getByText(/affect future roasts only/i)).toBeInTheDocument();
  });

  it("archives via the Archive button when onArchive is provided", async () => {
    const onArchive = vi.fn().mockResolvedValue({ id: FIXTURE_KOKE.id, result: "archived" });
    const onClose = vi.fn();
    render(
      <BeanProfileModal
        mode="edit"
        profile={FIXTURE_KOKE}
        onSave={vi.fn()}
        onSaved={vi.fn()}
        onClose={onClose}
        onArchive={onArchive}
      />,
    );
    fireEvent.click(screen.getByTestId("bean-profile-archive"));
    await waitFor(() => expect(onArchive).toHaveBeenCalledWith(FIXTURE_KOKE.id));
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  });

  it("closes without saving via Cancel", () => {
    const onClose = vi.fn();
    const onSave = vi.fn();
    render(
      <BeanProfileModal
        mode="edit"
        profile={FIXTURE_KOKE}
        onSave={onSave}
        onSaved={vi.fn()}
        onClose={onClose}
      />,
    );
    fireEvent.click(screen.getByTestId("bean-profile-cancel"));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onSave).not.toHaveBeenCalled();
  });
});

describe("BeanProfileModal — clears provenance on edit (#627 Codex round-2)", () => {
  // Neither "add" (DEFAULT_BEAN_PROFILE_DRAFT) nor "edit" (draftFromBeanProfile,
  // which never copies field_sources/field_evidence off a SAVED BeanProfile) seeds
  // provenance via this modal's own props, so these two spy on the shared
  // `withFieldEdited` helper (its clearing behaviour is exhaustively unit-tested in
  // beanProfileDraft.test.ts) to prove the REAL onChange/onBlendChange closures
  // delegate to it for an arbitrary field. The "draft-from-URL" describe block below
  // additionally proves the end-to-end case — a field seeded with REAL provenance
  // via the draft-from-URL response loses its badge on edit — closing the #627b seam
  // this comment used to describe as unreachable (#637).
  it("delegates a text/select field edit to withFieldEdited with the field name + new value", () => {
    const spy = vi.spyOn(beanProfileDraft, "withFieldEdited");
    render(
      <BeanProfileModal mode="add" onSave={vi.fn()} onSaved={vi.fn()} onClose={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("bean-profile-processing"), {
      target: { value: "washed" },
    });
    expect(spy).toHaveBeenCalledWith(expect.anything(), "processing", "washed");
  });

  it("delegates the blend-toggle edit to withFieldEdited with is_blend + the new boolean", () => {
    const spy = vi.spyOn(beanProfileDraft, "withFieldEdited");
    render(
      <BeanProfileModal
        mode="edit"
        profile={FIXTURE_KOKE}
        onSave={vi.fn()}
        onSaved={vi.fn()}
        onClose={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByTestId("bean-profile-is_blend"));
    expect(spy).toHaveBeenCalledWith(expect.anything(), "is_blend", !FIXTURE_KOKE.is_blend);
  });
});

describe("BeanProfileModal draft-from-URL (#573 phase 1, #627, #637)", () => {
  it("is offered in add mode but not in edit mode", () => {
    const { unmount } = render(
      <BeanProfileModal mode="add" onSave={vi.fn()} onSaved={vi.fn()} onClose={vi.fn()} />,
    );
    expect(screen.getByTestId("bean-profile-draft-url")).toBeInTheDocument();
    unmount();

    render(
      <BeanProfileModal
        mode="edit"
        profile={FIXTURE_KOKE}
        onSave={vi.fn()}
        onSaved={vi.fn()}
        onClose={vi.fn()}
      />,
    );
    expect(screen.queryByTestId("bean-profile-draft-url")).toBeNull();
  });

  it("disables the draft button until a URL is entered", () => {
    render(
      <BeanProfileModal mode="add" onSave={vi.fn()} onSaved={vi.fn()} onClose={vi.fn()} />,
    );
    expect(screen.getByTestId("bean-profile-draft-button")).toBeDisabled();
    fireEvent.change(screen.getByTestId("bean-profile-draft-url"), {
      target: { value: "https://roaster.example.com/guji-uraga" },
    });
    expect(screen.getByTestId("bean-profile-draft-button")).toBeEnabled();
  });

  it("drafts from a URL and seeds the form, lighting up the #627 provenance badges + evidence quote", async () => {
    const spy = vi.spyOn(api, "draftBeanFromUrl").mockResolvedValue(FIXTURE_DRAFT_RESPONSE);
    const { container } = render(
      <BeanProfileModal mode="add" onSave={vi.fn()} onSaved={vi.fn()} onClose={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("bean-profile-draft-url"), {
      target: { value: "https://roaster.example.com/guji-uraga" },
    });
    fireEvent.click(screen.getByTestId("bean-profile-draft-button"));

    await waitFor(() =>
      expect(spy).toHaveBeenCalledWith("https://roaster.example.com/guji-uraga"),
    );
    await waitFor(() =>
      expect(screen.getByTestId("bean-profile-name")).toHaveValue(FIXTURE_DRAFT_RESPONSE.name),
    );
    expect(screen.getByTestId("bean-profile-bean_origin")).toHaveValue(
      FIXTURE_DRAFT_RESPONSE.bean_origin,
    );
    expect(screen.getByTestId("bean-profile-processing")).toHaveValue("natural");
    expect(screen.getByTestId("bean-profile-altitude_m")).toHaveValue(2000);

    // The on-page typed field (processing) carries an "on page" badge + its captured quote.
    const processingBadge = container.querySelector("#bean-profile-processing-provenance");
    expect(processingBadge).toHaveAttribute("data-provenance", "on_page");
    expect(screen.getByTestId("bean-profile-processing-evidence-text")).toHaveTextContent(
      /naturally processed on raised beds/i,
    );

    // The origin-estimated typed field (altitude_m) carries a "review" badge and no quote.
    const altitudeBadge = container.querySelector("#bean-profile-altitude_m-provenance");
    expect(altitudeBadge).toHaveAttribute("data-provenance", "origin_estimated");
    expect(screen.queryByTestId("bean-profile-altitude_m-evidence")).toBeNull();

    // The conservative "scouting run" framing renders alongside the drafted targets.
    expect(screen.getByTestId("bean-profile-scouting-note")).toHaveTextContent(/scouting run/i);
  });

  it("editing a field seeded with real provenance clears its badge (closes the #627b seam)", async () => {
    vi.spyOn(api, "draftBeanFromUrl").mockResolvedValue(FIXTURE_DRAFT_RESPONSE);
    const { container } = render(
      <BeanProfileModal mode="add" onSave={vi.fn()} onSaved={vi.fn()} onClose={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("bean-profile-draft-url"), {
      target: { value: "https://roaster.example.com/guji-uraga" },
    });
    fireEvent.click(screen.getByTestId("bean-profile-draft-button"));
    await waitFor(() =>
      expect(
        container.querySelector("#bean-profile-processing-provenance"),
      ).not.toBeNull(),
    );

    fireEvent.change(screen.getByTestId("bean-profile-processing"), {
      target: { value: "washed" },
    });

    expect(container.querySelector("#bean-profile-processing-provenance")).toBeNull();
    expect(screen.queryByTestId("bean-profile-processing-evidence")).toBeNull();
    // A field the operator never touched keeps its badge.
    expect(container.querySelector("#bean-profile-altitude_m-provenance")).not.toBeNull();
  });

  it("maps a 422 to fix-the-input guidance, distinct from a 503", async () => {
    const spy = vi
      .spyOn(api, "draftBeanFromUrl")
      .mockRejectedValue(new ApiError(422, "the page yielded too little identity to draft from"));
    render(
      <BeanProfileModal mode="add" onSave={vi.fn()} onSaved={vi.fn()} onClose={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("bean-profile-draft-url"), {
      target: { value: "https://roaster.example.com/thin-page" },
    });
    fireEvent.click(screen.getByTestId("bean-profile-draft-button"));

    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
    expect(screen.getByTestId("bean-profile-draft-error")).toHaveTextContent(
      /check the url, or the page may be too thin/i,
    );
    // The name field was never seeded — the failed draft leaves the form untouched.
    expect(screen.getByTestId("bean-profile-name")).toHaveValue("");
  });

  it("maps a 503 to a provider-unavailable retry message, without dumping the raw detail", async () => {
    vi.spyOn(api, "draftBeanFromUrl").mockRejectedValue(
      new ApiError(503, "bean extraction temporarily unavailable (provider error): boom"),
    );
    render(
      <BeanProfileModal mode="add" onSave={vi.fn()} onSaved={vi.fn()} onClose={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("bean-profile-draft-url"), {
      target: { value: "https://roaster.example.com/guji-uraga" },
    });
    fireEvent.click(screen.getByTestId("bean-profile-draft-button"));

    await waitFor(() =>
      expect(screen.getByTestId("bean-profile-draft-error")).toHaveTextContent(
        /temporarily unavailable.*try again/i,
      ),
    );
    // The retry affordance is the same button, re-enabled once the request settles.
    expect(screen.getByTestId("bean-profile-draft-button")).toBeEnabled();
    expect(screen.getByTestId("bean-profile-draft-error")).not.toHaveTextContent(/boom/i);
  });

  it("maps a 409 (a roast is active) to the generic 'other' fallback, rendering the server's own detail", async () => {
    vi.spyOn(api, "draftBeanFromUrl").mockRejectedValue(
      new ApiError(409, "a roast is already active"),
    );
    render(
      <BeanProfileModal mode="add" onSave={vi.fn()} onSaved={vi.fn()} onClose={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("bean-profile-draft-url"), {
      target: { value: "https://roaster.example.com/guji-uraga" },
    });
    fireEvent.click(screen.getByTestId("bean-profile-draft-button"));

    await waitFor(() =>
      expect(screen.getByTestId("bean-profile-draft-error")).toHaveTextContent(
        /a roast is already active/i,
      ),
    );
  });

  it("maps a 429 (too many concurrent drafts) to the generic 'other' fallback, rendering the server's own detail", async () => {
    vi.spyOn(api, "draftBeanFromUrl").mockRejectedValue(
      new ApiError(429, "too many concurrent bean-draft requests in flight; try again shortly"),
    );
    render(
      <BeanProfileModal mode="add" onSave={vi.fn()} onSaved={vi.fn()} onClose={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("bean-profile-draft-url"), {
      target: { value: "https://roaster.example.com/guji-uraga" },
    });
    fireEvent.click(screen.getByTestId("bean-profile-draft-button"));

    await waitFor(() =>
      expect(screen.getByTestId("bean-profile-draft-error")).toHaveTextContent(
        /too many concurrent .* try again shortly/i,
      ),
    );
    // The retry affordance is the same button, re-enabled once the request settles.
    expect(screen.getByTestId("bean-profile-draft-button")).toBeEnabled();
  });

  it("a non-ApiError (network) failure falls back to the error's own generic message, never a raw exception dump", async () => {
    // A network-level failure (offline, DNS, CORS) rejects with a raw
    // browser-generated Error, never an ApiError — `fetch` itself throws
    // before the typed client ever sees an HTTP response to wrap.
    vi.spyOn(api, "draftBeanFromUrl").mockRejectedValue(new TypeError("Failed to fetch"));
    render(
      <BeanProfileModal mode="add" onSave={vi.fn()} onSaved={vi.fn()} onClose={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("bean-profile-draft-url"), {
      target: { value: "https://roaster.example.com/guji-uraga" },
    });
    fireEvent.click(screen.getByTestId("bean-profile-draft-button"));

    await waitFor(() =>
      expect(screen.getByTestId("bean-profile-draft-error")).toHaveTextContent(/failed to fetch/i),
    );
    // Nothing beyond the error's own short message ever renders — no stack
    // trace, no "TypeError:" prefix, no object dump.
    expect(screen.getByTestId("bean-profile-draft-error").textContent).toBe("Failed to fetch");
  });

  it("a thrown non-Error value falls back to the generic 'Request failed.' copy", async () => {
    // Defensive: `catch` can receive anything, not just an `Error` instance
    // (e.g. a rejected promise with a plain string/object) — the fallback
    // must still render something bounded, never `String(err)` of an
    // arbitrary thrown value.
    vi.spyOn(api, "draftBeanFromUrl").mockRejectedValue("boom, not an Error at all");
    render(
      <BeanProfileModal mode="add" onSave={vi.fn()} onSaved={vi.fn()} onClose={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("bean-profile-draft-url"), {
      target: { value: "https://roaster.example.com/guji-uraga" },
    });
    fireEvent.click(screen.getByTestId("bean-profile-draft-button"));

    await waitFor(() =>
      expect(screen.getByTestId("bean-profile-draft-error")).toHaveTextContent(/request failed/i),
    );
    expect(screen.getByTestId("bean-profile-draft-error")).not.toHaveTextContent(
      /boom, not an Error/i,
    );
  });

  it("clears a stale save error once a fresh draft applies successfully (#654 landing round, P3)", async () => {
    const onSave = vi.fn().mockRejectedValue(new ApiError(422, "invalid charge band"));
    vi.spyOn(api, "draftBeanFromUrl").mockResolvedValue(FIXTURE_DRAFT_RESPONSE);
    render(
      <BeanProfileModal mode="add" onSave={onSave} onSaved={vi.fn()} onClose={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("bean-profile-name"), { target: { value: "X" } });
    fireEvent.change(screen.getByTestId("bean-profile-bean_origin"), {
      target: { value: "Y" },
    });
    fireEvent.submit(screen.getByTestId("bean-profile-form"));
    await waitFor(() =>
      expect(screen.getByTestId("bean-profile-error")).toHaveTextContent(/invalid charge band/i),
    );

    fireEvent.change(screen.getByTestId("bean-profile-draft-url"), {
      target: { value: "https://roaster.example.com/guji-uraga" },
    });
    fireEvent.click(screen.getByTestId("bean-profile-draft-button"));
    await waitFor(() =>
      expect(screen.getByTestId("bean-profile-name")).toHaveValue(FIXTURE_DRAFT_RESPONSE.name),
    );
    // The stale save error must not caption the freshly-seeded draft.
    expect(screen.queryByTestId("bean-profile-error")).toBeNull();
  });
});

describe("BeanProfileModal draft-from-URL — Enter routes to draft, never submit (#654 fold 1)", () => {
  // jsdom does not itself implement a browser's implicit form-submission-on-Enter
  // for a text input, so `onSave` was never going to fire here even without the
  // fix — the load-bearing assertion is that Enter's default was prevented (the
  // ACTUAL mechanism that stops a real browser from submitting) and that it
  // routed to the draft action instead.
  it("Enter in the URL field (with a URL present) drafts and does not submit", async () => {
    const spy = vi.spyOn(api, "draftBeanFromUrl").mockResolvedValue(FIXTURE_DRAFT_RESPONSE);
    const onSave = vi.fn();
    render(
      <BeanProfileModal mode="add" onSave={onSave} onSaved={vi.fn()} onClose={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("bean-profile-draft-url"), {
      target: { value: "https://roaster.example.com/guji-uraga" },
    });
    const notDefaultPrevented = fireEvent.keyDown(screen.getByTestId("bean-profile-draft-url"), {
      key: "Enter",
    });
    expect(notDefaultPrevented).toBe(false);

    await waitFor(() =>
      expect(spy).toHaveBeenCalledWith("https://roaster.example.com/guji-uraga"),
    );
    expect(onSave).not.toHaveBeenCalled();
  });

  it("Enter after a prior successful draft still drafts, never saves", async () => {
    const spy = vi.spyOn(api, "draftBeanFromUrl").mockResolvedValue(FIXTURE_DRAFT_RESPONSE);
    const onSave = vi.fn();
    render(
      <BeanProfileModal mode="add" onSave={onSave} onSaved={vi.fn()} onClose={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("bean-profile-draft-url"), {
      target: { value: "https://roaster.example.com/first" },
    });
    fireEvent.click(screen.getByTestId("bean-profile-draft-button"));
    await waitFor(() =>
      expect(screen.getByTestId("bean-profile-name")).toHaveValue(FIXTURE_DRAFT_RESPONSE.name),
    );

    fireEvent.change(screen.getByTestId("bean-profile-draft-url"), {
      target: { value: "https://roaster.example.com/second" },
    });
    fireEvent.keyDown(screen.getByTestId("bean-profile-draft-url"), { key: "Enter" });

    await waitFor(() => expect(spy).toHaveBeenCalledTimes(2));
    expect(spy).toHaveBeenLastCalledWith("https://roaster.example.com/second");
    expect(onSave).not.toHaveBeenCalled();
  });
});

describe("BeanProfileModal draft-from-URL — tri-state is_blend blocks a silent false (#654 fold 2)", () => {
  it("blocks save with a clear message when the draft left is_blend unresolved (page never addressed it)", async () => {
    vi.spyOn(api, "draftBeanFromUrl").mockResolvedValue({
      ...FIXTURE_DRAFT_RESPONSE,
      is_blend: null,
    });
    const onSave = vi.fn();
    render(
      <BeanProfileModal mode="add" onSave={onSave} onSaved={vi.fn()} onClose={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("bean-profile-draft-url"), {
      target: { value: "https://roaster.example.com/guji-uraga" },
    });
    fireEvent.click(screen.getByTestId("bean-profile-draft-button"));
    await waitFor(() =>
      expect(screen.getByTestId("bean-profile-name")).toHaveValue(FIXTURE_DRAFT_RESPONSE.name),
    );

    // No plain checkbox while unresolved (#654 round 2 fold 2) — an explicit
    // two-choice control replaces it, since a checkbox's first interaction can
    // only ever SET it (confirming the safe default would be undiscoverable).
    // The cue is visible before any submit attempt.
    expect(screen.queryByTestId("bean-profile-is_blend")).toBeNull();
    expect(screen.getByTestId("bean-profile-is_blend-choose-single-origin")).toBeInTheDocument();
    expect(screen.getByTestId("bean-profile-is_blend-choose-blend")).toBeInTheDocument();
    expect(screen.getByTestId("bean-profile-is_blend-unresolved")).toBeInTheDocument();

    fireEvent.submit(screen.getByTestId("bean-profile-form"));
    expect(onSave).not.toHaveBeenCalled();
    expect(screen.getByTestId("bean-profile-is_blend-error")).toHaveTextContent(
      /didn't say|choose/i,
    );
  });

  it("saving is unblocked once the operator explicitly chooses Single origin, and the plain checkbox returns", async () => {
    vi.spyOn(api, "draftBeanFromUrl").mockResolvedValue({
      ...FIXTURE_DRAFT_RESPONSE,
      is_blend: null,
    });
    const onSave = vi.fn(async (input: BeanProfileInput) => savedFrom(input));
    render(
      <BeanProfileModal mode="add" onSave={onSave} onSaved={vi.fn()} onClose={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("bean-profile-draft-url"), {
      target: { value: "https://roaster.example.com/guji-uraga" },
    });
    fireEvent.click(screen.getByTestId("bean-profile-draft-button"));
    await waitFor(() =>
      expect(screen.getByTestId("bean-profile-name")).toHaveValue(FIXTURE_DRAFT_RESPONSE.name),
    );

    fireEvent.click(screen.getByTestId("bean-profile-is_blend-choose-single-origin"));
    expect(screen.queryByTestId("bean-profile-is_blend-unresolved")).toBeNull();
    // The plain checkbox returns, unchecked (the chosen value).
    expect(screen.getByTestId("bean-profile-is_blend")).not.toBeChecked();

    fireEvent.submit(screen.getByTestId("bean-profile-form"));
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave.mock.calls[0][0].is_blend).toBe(false);
  });

  it("saving is unblocked once the operator explicitly chooses Blend", async () => {
    vi.spyOn(api, "draftBeanFromUrl").mockResolvedValue({
      ...FIXTURE_DRAFT_RESPONSE,
      is_blend: null,
    });
    const onSave = vi.fn(async (input: BeanProfileInput) => savedFrom(input));
    render(
      <BeanProfileModal mode="add" onSave={onSave} onSaved={vi.fn()} onClose={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("bean-profile-draft-url"), {
      target: { value: "https://roaster.example.com/guji-uraga" },
    });
    fireEvent.click(screen.getByTestId("bean-profile-draft-button"));
    await waitFor(() =>
      expect(screen.getByTestId("bean-profile-name")).toHaveValue(FIXTURE_DRAFT_RESPONSE.name),
    );

    fireEvent.click(screen.getByTestId("bean-profile-is_blend-choose-blend"));
    expect(screen.queryByTestId("bean-profile-is_blend-unresolved")).toBeNull();
    expect(screen.getByTestId("bean-profile-is_blend")).toBeChecked();

    fireEvent.submit(screen.getByTestId("bean-profile-form"));
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave.mock.calls[0][0].is_blend).toBe(true);
  });

  it("explicit true/false from the draft (not null) never blocks save", async () => {
    vi.spyOn(api, "draftBeanFromUrl").mockResolvedValue({
      ...FIXTURE_DRAFT_RESPONSE,
      is_blend: false,
    });
    const onSave = vi.fn(async (input: BeanProfileInput) => savedFrom(input));
    render(
      <BeanProfileModal mode="add" onSave={onSave} onSaved={vi.fn()} onClose={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("bean-profile-draft-url"), {
      target: { value: "https://roaster.example.com/guji-uraga" },
    });
    fireEvent.click(screen.getByTestId("bean-profile-draft-button"));
    await waitFor(() =>
      expect(screen.getByTestId("bean-profile-name")).toHaveValue(FIXTURE_DRAFT_RESPONSE.name),
    );

    expect(screen.queryByTestId("bean-profile-is_blend-unresolved")).toBeNull();
    fireEvent.submit(screen.getByTestId("bean-profile-form"));
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave.mock.calls[0][0].is_blend).toBe(false);
  });

  it("clears the is_blend validation error immediately on the explicit choice, not only at next save (#654 final round)", async () => {
    vi.spyOn(api, "draftBeanFromUrl").mockResolvedValue({
      ...FIXTURE_DRAFT_RESPONSE,
      is_blend: null,
    });
    render(
      <BeanProfileModal mode="add" onSave={vi.fn()} onSaved={vi.fn()} onClose={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("bean-profile-draft-url"), {
      target: { value: "https://roaster.example.com/guji-uraga" },
    });
    fireEvent.click(screen.getByTestId("bean-profile-draft-button"));
    await waitFor(() =>
      expect(screen.getByTestId("bean-profile-name")).toHaveValue(FIXTURE_DRAFT_RESPONSE.name),
    );

    // A failed submit attempt surfaces the error first.
    fireEvent.submit(screen.getByTestId("bean-profile-form"));
    expect(screen.getByTestId("bean-profile-is_blend-error")).toBeInTheDocument();

    // Choosing either option clears it right away — no need to hit Save again.
    fireEvent.click(screen.getByTestId("bean-profile-is_blend-choose-single-origin"));
    expect(screen.queryByTestId("bean-profile-is_blend-error")).toBeNull();
  });
});

describe("BeanProfileModal draft-from-URL — the scouting note stays paired with its draft (#654 fold 3)", () => {
  it("a failed retry (422/503) leaves the prior successful draft's note visible", async () => {
    const spy = vi.spyOn(api, "draftBeanFromUrl");
    spy.mockResolvedValueOnce(FIXTURE_DRAFT_RESPONSE);
    render(
      <BeanProfileModal mode="add" onSave={vi.fn()} onSaved={vi.fn()} onClose={vi.fn()} />,
    );

    fireEvent.change(screen.getByTestId("bean-profile-draft-url"), {
      target: { value: "https://roaster.example.com/a" },
    });
    fireEvent.click(screen.getByTestId("bean-profile-draft-button"));
    await waitFor(() =>
      expect(screen.getByTestId("bean-profile-scouting-note")).toHaveTextContent(
        FIXTURE_DRAFT_RESPONSE.scouting_note,
      ),
    );

    spy.mockRejectedValueOnce(new ApiError(422, "the page yielded too little identity"));
    fireEvent.change(screen.getByTestId("bean-profile-draft-url"), {
      target: { value: "https://roaster.example.com/b" },
    });
    fireEvent.click(screen.getByTestId("bean-profile-draft-button"));
    await waitFor(() =>
      expect(screen.getByTestId("bean-profile-draft-error")).toBeInTheDocument(),
    );

    // A's note (and its seeded fields) are still active — B's failed attempt
    // never touched them.
    expect(screen.getByTestId("bean-profile-scouting-note")).toHaveTextContent(
      FIXTURE_DRAFT_RESPONSE.scouting_note,
    );
    expect(screen.getByTestId("bean-profile-name")).toHaveValue(FIXTURE_DRAFT_RESPONSE.name);
  });

  it("retires the note when the operator edits a field it summarizes (drop temperature) — no stale recompute (#654 final round)", async () => {
    vi.spyOn(api, "draftBeanFromUrl").mockResolvedValue(FIXTURE_DRAFT_RESPONSE);
    render(
      <BeanProfileModal mode="add" onSave={vi.fn()} onSaved={vi.fn()} onClose={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("bean-profile-draft-url"), {
      target: { value: "https://roaster.example.com/a" },
    });
    fireEvent.click(screen.getByTestId("bean-profile-draft-button"));
    await waitFor(() =>
      expect(screen.getByTestId("bean-profile-scouting-note")).toBeInTheDocument(),
    );

    fireEvent.change(screen.getByTestId("bean-profile-target_drop_temp_c"), {
      target: { value: "192" },
    });
    expect(screen.queryByTestId("bean-profile-scouting-note")).toBeNull();
  });

  it("keeps the note when the operator edits an unrelated field (farm)", async () => {
    vi.spyOn(api, "draftBeanFromUrl").mockResolvedValue(FIXTURE_DRAFT_RESPONSE);
    render(
      <BeanProfileModal mode="add" onSave={vi.fn()} onSaved={vi.fn()} onClose={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("bean-profile-draft-url"), {
      target: { value: "https://roaster.example.com/a" },
    });
    fireEvent.click(screen.getByTestId("bean-profile-draft-button"));
    await waitFor(() =>
      expect(screen.getByTestId("bean-profile-scouting-note")).toBeInTheDocument(),
    );

    fireEvent.change(screen.getByTestId("bean-profile-farm"), {
      target: { value: "A different washing station" },
    });
    expect(screen.getByTestId("bean-profile-scouting-note")).toBeInTheDocument();
  });
});

describe("BeanProfileModal draft-from-URL — in-flight race guard (#654 fold 4, round 2 folds 1 + 3)", () => {
  it("a same-batch double-fire (rapid double-click/Enter) produces exactly ONE request, not two (round 2 fold 1)", async () => {
    // Before the synchronous `draftingRef` guard, two dispatches landing in the
    // SAME event batch both read the stale `drafting === false` and both fired
    // a real request — the backend's own one-at-a-time admission then 429'd the
    // second, and that error could race the first's genuine success for the
    // (bumped) token. The ref closes the window: only ONE request is ever sent.
    const pending = deferred<BeanProfileDraftResponse>();
    const spy = vi.spyOn(api, "draftBeanFromUrl").mockReturnValue(pending.promise);
    render(
      <BeanProfileModal mode="add" onSave={vi.fn()} onSaved={vi.fn()} onClose={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("bean-profile-draft-url"), {
      target: { value: "https://roaster.example.com/race" },
    });
    fireOverlappingDraftRequests(
      screen.getByTestId("bean-profile-draft-button"),
      screen.getByTestId("bean-profile-draft-url"),
    );
    expect(spy).toHaveBeenCalledTimes(1);

    await act(async () => {
      pending.resolve(FIXTURE_DRAFT_RESPONSE);
    });
    await waitFor(() =>
      expect(screen.getByTestId("bean-profile-name")).toHaveValue(FIXTURE_DRAFT_RESPONSE.name),
    );
  });

  it("disables Save while a draft is in flight, re-enabling once it settles", async () => {
    const pending = deferred<BeanProfileDraftResponse>();
    vi.spyOn(api, "draftBeanFromUrl").mockReturnValue(pending.promise);
    render(
      <BeanProfileModal mode="add" onSave={vi.fn()} onSaved={vi.fn()} onClose={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("bean-profile-draft-url"), {
      target: { value: "https://roaster.example.com/x" },
    });
    fireEvent.click(screen.getByTestId("bean-profile-draft-button"));
    expect(screen.getByTestId("bean-profile-save")).toBeDisabled();

    await act(async () => {
      pending.resolve(FIXTURE_DRAFT_RESPONSE);
    });
    await waitFor(() => expect(screen.getByTestId("bean-profile-save")).toBeEnabled());
  });

  it("editing a field while a draft is in flight invalidates its DATA, but the single-flight guard stays held until the old promise settles (#654 landing round)", async () => {
    const pending = deferred<BeanProfileDraftResponse>();
    vi.spyOn(api, "draftBeanFromUrl").mockReturnValue(pending.promise);
    render(
      <BeanProfileModal mode="add" onSave={vi.fn()} onSaved={vi.fn()} onClose={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("bean-profile-draft-url"), {
      target: { value: "https://roaster.example.com/x" },
    });
    fireEvent.click(screen.getByTestId("bean-profile-draft-button"));
    expect(screen.getByTestId("bean-profile-save")).toBeDisabled();

    // The operator edits a field WHILE the draft is in flight.
    fireEvent.change(screen.getByTestId("bean-profile-name"), {
      target: { value: "Operator edit mid-flight" },
    });
    // Save stays DISABLED (#654 landing round): the backend has no disconnect
    // check, so it keeps holding its one-at-a-time slot until the abandoned
    // request actually finishes — releasing the guard here would risk a
    // self-inflicted 429 on a fresh attempt fired too soon.
    expect(screen.getByTestId("bean-profile-save")).toBeDisabled();

    await act(async () => {
      pending.resolve({ ...FIXTURE_DRAFT_RESPONSE, name: "Stale draft" });
    });
    // Now that the old request has settled, the guard releases.
    await waitFor(() => expect(screen.getByTestId("bean-profile-save")).toBeEnabled());
    // The edit stands; the response's DATA never applied at all (no partial
    // application — the scouting note it would have carried is absent too).
    expect(screen.getByTestId("bean-profile-name")).toHaveValue("Operator edit mid-flight");
    expect(screen.queryByTestId("bean-profile-scouting-note")).toBeNull();
  });

  it("choosing Single origin / Blend while unresolved also invalidates an in-flight draft's DATA, guard held the same way (#654 round 2 fold 3, landing round)", async () => {
    // A prior successful draft left is_blend unresolved; a SECOND draft attempt
    // (e.g. a retry) is in flight when the operator resolves the blend choice
    // from the first draft — that edit must invalidate the second attempt's
    // DATA too, though the guard still waits for it to settle.
    const spy = vi.spyOn(api, "draftBeanFromUrl");
    spy.mockResolvedValueOnce({ ...FIXTURE_DRAFT_RESPONSE, is_blend: null });
    render(
      <BeanProfileModal mode="add" onSave={vi.fn()} onSaved={vi.fn()} onClose={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("bean-profile-draft-url"), {
      target: { value: "https://roaster.example.com/a" },
    });
    fireEvent.click(screen.getByTestId("bean-profile-draft-button"));
    await waitFor(() =>
      expect(screen.getByTestId("bean-profile-is_blend-unresolved")).toBeInTheDocument(),
    );

    const pending = deferred<BeanProfileDraftResponse>();
    spy.mockReturnValueOnce(pending.promise);
    fireEvent.change(screen.getByTestId("bean-profile-draft-url"), {
      target: { value: "https://roaster.example.com/b" },
    });
    fireEvent.click(screen.getByTestId("bean-profile-draft-button"));
    expect(screen.getByTestId("bean-profile-save")).toBeDisabled();

    fireEvent.click(screen.getByTestId("bean-profile-is_blend-choose-single-origin"));
    // Guard stays held — the second (retry) request is still running server-
    // side as far as we know.
    expect(screen.getByTestId("bean-profile-save")).toBeDisabled();

    await act(async () => {
      pending.resolve({ ...FIXTURE_DRAFT_RESPONSE, name: "Stale retry" });
    });
    await waitFor(() => expect(screen.getByTestId("bean-profile-save")).toBeEnabled());
    expect(screen.getByTestId("bean-profile-name")).toHaveValue(FIXTURE_DRAFT_RESPONSE.name);
    expect(screen.queryByTestId("bean-profile-is_blend-unresolved")).toBeNull();
  });

  it("editing the URL mid-flight invalidates the old request's data; a new draft attempt is refused until the old promise settles, THEN fires (#654 final round, landing round)", async () => {
    const first = deferred<BeanProfileDraftResponse>();
    const spy = vi.spyOn(api, "draftBeanFromUrl").mockReturnValue(first.promise);
    render(
      <BeanProfileModal mode="add" onSave={vi.fn()} onSaved={vi.fn()} onClose={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("bean-profile-draft-url"), {
      target: { value: "https://roaster.example.com/old" },
    });
    fireEvent.click(screen.getByTestId("bean-profile-draft-button"));
    expect(screen.getByTestId("bean-profile-save")).toBeDisabled();
    expect(spy).toHaveBeenCalledTimes(1);

    // Editing the URL WHILE the first request is in flight invalidates its
    // DATA, but the single-flight guard stays held (#654 landing round).
    fireEvent.change(screen.getByTestId("bean-profile-draft-url"), {
      target: { value: "https://roaster.example.com/new" },
    });
    expect(screen.getByTestId("bean-profile-save")).toBeDisabled();

    // A new draft attempt (Enter on the new URL) is REFUSED while the old
    // request is still unsettled — no second call fires yet.
    fireEvent.keyDown(screen.getByTestId("bean-profile-draft-url"), { key: "Enter" });
    expect(spy).toHaveBeenCalledTimes(1);

    // The stale first (old-url) response finally settles — the guard
    // releases, and its DATA is dropped (superseded token).
    const second = deferred<BeanProfileDraftResponse>();
    spy.mockReturnValueOnce(second.promise);
    await act(async () => {
      first.resolve({ ...FIXTURE_DRAFT_RESPONSE, name: "Stale (old url)" });
    });
    await waitFor(() => expect(screen.getByTestId("bean-profile-save")).toBeEnabled());
    expect(screen.getByTestId("bean-profile-name")).not.toHaveValue("Stale (old url)");

    // NOW a fresh Enter on the new URL fires a genuinely new request.
    fireEvent.keyDown(screen.getByTestId("bean-profile-draft-url"), { key: "Enter" });
    expect(spy).toHaveBeenCalledTimes(2);
    expect(spy).toHaveBeenLastCalledWith("https://roaster.example.com/new");

    await act(async () => {
      second.resolve({ ...FIXTURE_DRAFT_RESPONSE, name: "Fresh (new url)" });
    });
    await waitFor(() =>
      expect(screen.getByTestId("bean-profile-name")).toHaveValue("Fresh (new url)"),
    );
  });

  it("invalidation is token-bump only — no abort, no premature guard release (#654 verdict round): a new draft attempt is refused until the ORIGINAL response arrives, its stale data is dropped, then a fresh draft fires", async () => {
    // An earlier version aborted the fetch here. That was self-defeating:
    // AbortController.abort() settles the fetch's promise IMMEDIATELY on the
    // client, so the guard would have released (via handleDraftFromUrl's own
    // finally, triggered by that abort-rejection) long before the backend —
    // which has no disconnect check on this route — actually finished with
    // its one-at-a-time admission slot. Dropping the abort and letting the
    // guard hold for the REAL response is what makes the hold correct.
    const pending = deferred<BeanProfileDraftResponse>();
    const spy = vi.spyOn(api, "draftBeanFromUrl").mockReturnValue(pending.promise);
    render(
      <BeanProfileModal mode="add" onSave={vi.fn()} onSaved={vi.fn()} onClose={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("bean-profile-draft-url"), {
      target: { value: "https://roaster.example.com/x" },
    });
    fireEvent.click(screen.getByTestId("bean-profile-draft-button"));
    expect(spy).toHaveBeenCalledTimes(1);
    // Called with just the URL — no AbortSignal is threaded through here.
    expect(spy).toHaveBeenCalledWith("https://roaster.example.com/x");

    fireEvent.change(screen.getByTestId("bean-profile-name"), {
      target: { value: "Operator edit mid-flight" },
    });
    // The guard holds: a second attempt is refused while the original is
    // still unsettled.
    expect(screen.getByTestId("bean-profile-save")).toBeDisabled();
    fireEvent.click(screen.getByTestId("bean-profile-draft-button"));
    expect(spy).toHaveBeenCalledTimes(1);

    // The ORIGINAL request finally settles — its (now-stale) data is dropped,
    // and the guard releases only now.
    await act(async () => {
      pending.resolve({ ...FIXTURE_DRAFT_RESPONSE, name: "Stale draft" });
    });
    await waitFor(() => expect(screen.getByTestId("bean-profile-save")).toBeEnabled());
    expect(screen.getByTestId("bean-profile-name")).toHaveValue("Operator edit mid-flight");

    // A fresh draft attempt now succeeds.
    const second = deferred<BeanProfileDraftResponse>();
    spy.mockReturnValueOnce(second.promise);
    fireEvent.click(screen.getByTestId("bean-profile-draft-button"));
    expect(spy).toHaveBeenCalledTimes(2);
    await act(async () => {
      second.resolve({ ...FIXTURE_DRAFT_RESPONSE, name: "Fresh draft" });
    });
    await waitFor(() =>
      expect(screen.getByTestId("bean-profile-name")).toHaveValue("Fresh draft"),
    );
  });

  it("blocks a draft attempt while a save is in flight (#654 landing round): no vendor/LLM call starts mid-save", async () => {
    const savePending = deferred<BeanProfile>();
    const onSave = vi.fn(() => savePending.promise);
    const draftSpy = vi.spyOn(api, "draftBeanFromUrl");
    render(
      <BeanProfileModal mode="add" onSave={onSave} onSaved={vi.fn()} onClose={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("bean-profile-name"), { target: { value: "X" } });
    fireEvent.change(screen.getByTestId("bean-profile-bean_origin"), {
      target: { value: "Y" },
    });
    fireEvent.submit(screen.getByTestId("bean-profile-form"));
    expect(screen.getByTestId("bean-profile-save")).toBeDisabled();

    fireEvent.change(screen.getByTestId("bean-profile-draft-url"), {
      target: { value: "https://roaster.example.com/x" },
    });
    expect(screen.getByTestId("bean-profile-draft-button")).toBeDisabled();
    fireEvent.click(screen.getByTestId("bean-profile-draft-button"));
    fireEvent.keyDown(screen.getByTestId("bean-profile-draft-url"), { key: "Enter" });
    expect(draftSpy).not.toHaveBeenCalled();

    await act(async () => {
      savePending.resolve(
        savedFrom({
          name: "X",
          bean_origin: "Y",
          bean_varietal: null,
          charge_guidance_min_c: 170,
          charge_guidance_max_c: 200,
          initial_heat_percent: 70,
          initial_fan_percent: 40,
          target_drop_temp_c: 195,
          target_development_percent: 15,
          default_bean_weight_grams: 250,
        }),
      );
    });
  });
});

describe("BeanProfileModal draft-from-URL — the single-flight guard survives unmount (#654 final thread)", () => {
  it("a request abandoned by Cancel (unmount) still blocks a remounted modal's draft attempt, until it settles a fresh draft fires", async () => {
    // Cancel unmounts the modal while the request is still running server-
    // side (no disconnect check on this route, #654 verdict round) — a
    // per-instance guard would reset to idle on the fresh instance below and
    // fire straight into the backend's still-occupied one-at-a-time slot.
    const abandoned = deferred<BeanProfileDraftResponse>();
    const spy = vi.spyOn(api, "draftBeanFromUrl").mockReturnValue(abandoned.promise);
    const first = render(
      <BeanProfileModal mode="add" onSave={vi.fn()} onSaved={vi.fn()} onClose={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("bean-profile-draft-url"), {
      target: { value: "https://roaster.example.com/abandoned" },
    });
    fireEvent.click(screen.getByTestId("bean-profile-draft-button"));
    expect(spy).toHaveBeenCalledTimes(1);

    first.unmount();
    render(<BeanProfileModal mode="add" onSave={vi.fn()} onSaved={vi.fn()} onClose={vi.fn()} />);

    // The remounted instance adopts the module-level in-flight status.
    expect(screen.getByTestId("bean-profile-draft-button")).toBeDisabled();
    fireEvent.change(screen.getByTestId("bean-profile-draft-url"), {
      target: { value: "https://roaster.example.com/new" },
    });
    // Enter routes straight to `handleDraftFromUrl`'s own guard, regardless
    // of the button's disabled attribute — a second real request is refused.
    fireEvent.keyDown(screen.getByTestId("bean-profile-draft-url"), { key: "Enter" });
    expect(spy).toHaveBeenCalledTimes(1);

    // The abandoned request finally settles — the (unmounted) first instance
    // never touches state to apply it, and the guard releases.
    const fresh = deferred<BeanProfileDraftResponse>();
    spy.mockReturnValueOnce(fresh.promise);
    await act(async () => {
      abandoned.resolve({ ...FIXTURE_DRAFT_RESPONSE, name: "Abandoned draft" });
    });
    await waitFor(() => expect(screen.getByTestId("bean-profile-draft-button")).toBeEnabled());

    // Now a fresh draft attempt in the remounted instance fires for real.
    fireEvent.keyDown(screen.getByTestId("bean-profile-draft-url"), { key: "Enter" });
    expect(spy).toHaveBeenCalledTimes(2);
    expect(spy).toHaveBeenLastCalledWith("https://roaster.example.com/new");
    await act(async () => {
      fresh.resolve({ ...FIXTURE_DRAFT_RESPONSE, name: "Fresh after remount" });
    });
    await waitFor(() =>
      expect(screen.getByTestId("bean-profile-name")).toHaveValue("Fresh after remount"),
    );
  });

  it("produces no setState-on-an-unmounted-component warning when the abandoned request settles after unmount", async () => {
    const pending = deferred<BeanProfileDraftResponse>();
    vi.spyOn(api, "draftBeanFromUrl").mockReturnValue(pending.promise);
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    const view = render(
      <BeanProfileModal mode="add" onSave={vi.fn()} onSaved={vi.fn()} onClose={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("bean-profile-draft-url"), {
      target: { value: "https://roaster.example.com/x" },
    });
    fireEvent.click(screen.getByTestId("bean-profile-draft-button"));
    view.unmount();

    await act(async () => {
      pending.resolve(FIXTURE_DRAFT_RESPONSE);
    });

    const unmountWarning = consoleError.mock.calls.some((args) =>
      args.some((arg) => typeof arg === "string" && arg.includes("unmounted component")),
    );
    expect(unmountWarning).toBe(false);
  });
});

describe("BeanProfileModal draft-from-URL — redacts URL query strings in the error detail (#654 round 2 fold 4)", () => {
  it("a 422 detail embedding a signed URL renders without its query string", async () => {
    vi.spyOn(api, "draftBeanFromUrl").mockRejectedValue(
      new ApiError(
        422,
        "drafted bean profile failed validation for 'https://x.test/p?token=abc': bad",
      ),
    );
    render(
      <BeanProfileModal mode="add" onSave={vi.fn()} onSaved={vi.fn()} onClose={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("bean-profile-draft-url"), {
      target: { value: "https://x.test/p?token=abc" },
    });
    fireEvent.click(screen.getByTestId("bean-profile-draft-button"));

    await waitFor(() =>
      expect(screen.getByTestId("bean-profile-draft-error")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("bean-profile-draft-error")).not.toHaveTextContent("token=abc");
    expect(screen.getByTestId("bean-profile-draft-error")).toHaveTextContent(
      "https://x.test/p",
    );
  });
});

describe("BeanProfileModal draft-from-URL — strips bidi controls from drafted free text (#654 round 2 fold 6)", () => {
  const RLO = String.fromCodePoint(0x202e); // RIGHT-TO-LEFT OVERRIDE

  it("a bidi-override-bearing drafted name renders stripped and saves stripped", async () => {
    vi.spyOn(api, "draftBeanFromUrl").mockResolvedValue({
      ...FIXTURE_DRAFT_RESPONSE,
      name: `${RLO}Hostile Name`,
    });
    const onSave = vi.fn(async (input: BeanProfileInput) => savedFrom(input));
    render(
      <BeanProfileModal mode="add" onSave={onSave} onSaved={vi.fn()} onClose={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("bean-profile-draft-url"), {
      target: { value: "https://roaster.example.com/guji-uraga" },
    });
    fireEvent.click(screen.getByTestId("bean-profile-draft-button"));

    await waitFor(() =>
      expect(screen.getByTestId("bean-profile-name")).toHaveValue("Hostile Name"),
    );
    expect((screen.getByTestId("bean-profile-name") as HTMLInputElement).value).not.toContain(
      RLO,
    );

    fireEvent.submit(screen.getByTestId("bean-profile-form"));
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave.mock.calls[0][0].name).toBe("Hostile Name");
  });
});

describe("BeanProfileModal draft-from-URL — stacks the URL/button row below `sm` (#654 final round)", () => {
  // Vitest/jsdom can't evaluate media queries, so this is a class-presence
  // assertion (Tailwind's `sm:` variants), not a rendered-layout assertion —
  // the scoped Playwright baseline (captured at the 1600px desktop viewport,
  // well above `sm`) is the pixel-level check, and stays unaffected by this.
  it("the row container collapses to a column below `sm`", () => {
    render(
      <BeanProfileModal mode="add" onSave={vi.fn()} onSaved={vi.fn()} onClose={vi.fn()} />,
    );
    const row = screen.getByTestId("bean-profile-draft-url").parentElement;
    expect(row).not.toBeNull();
    expect(row).toHaveClass("flex-col");
    expect(row).toHaveClass("sm:flex-row");
  });

  it("the input is full-width below `sm` and shares the row at `sm`+", () => {
    render(
      <BeanProfileModal mode="add" onSave={vi.fn()} onSaved={vi.fn()} onClose={vi.fn()} />,
    );
    const input = screen.getByTestId("bean-profile-draft-url");
    expect(input).toHaveClass("w-full");
    expect(input).toHaveClass("sm:flex-1");
  });

  it("the button is full-width below `sm` and auto-width at `sm`+", () => {
    render(
      <BeanProfileModal mode="add" onSave={vi.fn()} onSaved={vi.fn()} onClose={vi.fn()} />,
    );
    const button = screen.getByTestId("bean-profile-draft-button");
    expect(button).toHaveClass("w-full");
    expect(button).toHaveClass("sm:w-auto");
  });
});
