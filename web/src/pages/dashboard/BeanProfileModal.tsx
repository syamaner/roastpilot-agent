/**
 * Add / edit a saved bean profile (#303, D45).
 *
 * A blocking modal over the Start-Roast page that captures the full bean-profile
 * field set (the shared `BeanProfileFields`) plus the `default_bean_weight_grams`
 * that pre-fills a new roast's charge weight. On save it POSTs (add) or PUTs (edit)
 * via the typed bean-profile mutations; the parent invalidates the list query and
 * selects the saved profile. Editing a saved profile affects FUTURE roasts only —
 * the backend guarantees a past roast keeps its frozen snapshot (#303).
 *
 * Add mode also offers "Draft from a vendor page" (#573 phase 1, #637): a URL
 * calls `POST /api/beans/draft-from-url`, and the returned draft — including its
 * `field_sources`/`field_evidence` — seeds the form below, which activates the
 * existing provenance-badge/evidence-quote UI (#627) with no further wiring.
 * Drafting never saves anything; the operator still reviews/edits and submits
 * normally. Edit mode omits the affordance (re-drafting over an already-saved
 * profile is out of this slice's scope).
 *
 * INVARIANTS: the SPA renders + mutates only via the typed REST client (never MCP);
 * all temperatures are Celsius; the client-side validation is defense-in-depth + UX,
 * the server's pydantic bounds are the authority (a 422 surfaces inline).
 */

import { useEffect, useRef, useState } from "react";

import { api, ApiError } from "@/lib/api";
import { cn } from "@/lib/cn";
import type { BeanProfile, BeanProfileInput } from "@/lib/types";
import {
  DEFAULT_BEAN_PROFILE_DRAFT,
  draftFromBeanProfile,
  draftFromBeanProfileDraft,
  redactUrlQueryStrings,
  validateBeanProfile,
  withFieldEdited,
  type BeanProfileDraft,
  type BeanProfileErrors,
} from "./beanProfileDraft";
import { BeanProfileFields } from "./beanProfileFields";

/**
 * Module-scope single-flight tracking for draft-from-URL (#654 final
 * thread) — plain module state, NOT per-component-instance. Cancelling (or
 * any other unmount) destroys a per-instance ref along with the component,
 * but the backend has no disconnect check on this route (#654 verdict
 * round): an abandoned request may still be running server-side, still
 * holding its one-at-a-time admission slot, when the modal is reopened. A
 * freshly-mounted instance's own state starts clean and would otherwise
 * fire straight into that slot. `settle` resolves once the CURRENT
 * request's fetch actually settles, regardless of which instance (or
 * whether any instance) is still mounted to see it — a remounted instance
 * subscribes to it to adopt the busy state it can't otherwise see.
 */
let draftInFlight: { settle: Promise<void> } | null = null;

/** The draft fields the server's `scouting_note` text actually summarizes
 *  (#654 final round): "...targets are a conservative, de-risked starting
 *  point (X % development, drop Y °C) based on the Z processing method...".
 *  Editing any ONE of these three retires the note — it would otherwise
 *  keep citing figures the operator has since changed, and there is no
 *  cheap way to recompute the prose client-side. Editing an unrelated field
 *  (e.g. `farm`) leaves the note in place. */
const SCOUTING_NOTE_SUMMARIZED_FIELDS = new Set<keyof BeanProfileDraft>([
  "processing",
  "target_development_percent",
  "target_drop_temp_c",
]);

export interface BeanProfileModalProps {
  /** Modal mode — "add" starts blank; "edit" pre-fills from `profile`. */
  mode: "add" | "edit";
  /** The profile being edited (required in "edit" mode); ignored in "add". */
  profile?: BeanProfile;
  /** Persist the captured input — wired to the create/update mutation. Resolves to
   *  the saved `BeanProfile`; rejects (ApiError) on a 4xx/5xx (e.g. 422). */
  onSave: (input: BeanProfileInput, draftAttemptId?: string) => Promise<BeanProfile>;
  /** Called with the saved profile so the parent can select it + close. */
  onSaved: (saved: BeanProfile) => void;
  /** Close without saving. */
  onClose: () => void;
  /** Archive (soft-delete) the edited profile — wired to the delete mutation.
   *  Only offered in "edit" mode; omit to hide the affordance. */
  onArchive?: (id: string) => Promise<unknown>;
}

export function BeanProfileModal({
  mode,
  profile,
  onSave,
  onSaved,
  onClose,
  onArchive,
}: BeanProfileModalProps): React.JSX.Element {
  const [draft, setDraft] = useState<BeanProfileDraft>(() =>
    mode === "edit" && profile !== undefined
      ? draftFromBeanProfile(profile)
      : DEFAULT_BEAN_PROFILE_DRAFT,
  );
  const [errors, setErrors] = useState<BeanProfileErrors>({});
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  // Draft-from-URL (#573 phase 1, #637): a scouting entry that fetches a vendor
  // page and seeds the form below from the drafted profile — nothing is saved
  // until the operator reviews it and submits normally. `draftErrorKind`
  // distinguishes the two origin-mapped failure modes (#613) the endpoint can
  // return, so the copy tells the operator whether the URL/page is the problem
  // or the extraction provider is: a 422 is fix-the-input, a 503 is
  // try-again-shortly, and anything else (409/429/network) is a generic
  // fail-and-retry. `drafting` doubles as the retry affordance — the same
  // button re-fires the request with whatever URL is still in the field.
  const [draftUrl, setDraftUrl] = useState("");
  const [drafting, setDrafting] = useState(false);
  const [draftErrorKind, setDraftErrorKind] = useState<"invalid" | "unavailable" | "other" | null>(
    null,
  );
  const [draftErrorDetail, setDraftErrorDetail] = useState<string | null>(null);
  const [scoutingNote, setScoutingNote] = useState<string | null>(null);
  const [draftAttemptId, setDraftAttemptId] = useState<string | undefined>(undefined);
  // Race guard (#637, #654 round 2): the latest fired request "wins" — a token
  // bumped whenever the in-flight draft is invalidated (a newer request, or an
  // operator edit made while it was pending), captured at fire time, and
  // re-checked before EITHER branch of `handleDraftFromUrl` applies its result.
  // A response that no longer matches the current token is dropped outright.
  const draftRequestIdRef = useRef(0);
  // Whether THIS instance is still mounted (#654 final thread): every
  // setState past an `await` in `handleDraftFromUrl` is gated on this too, so
  // an instance that unmounted mid-request (Cancel) never touches state that
  // no longer exists once its own abandoned response finally arrives.
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);
  // On mount, adopt whatever module-level in-flight status already exists
  // (#654 final thread): a remounted modal (e.g. reopened via Cancel while a
  // request was still running) must show itself as busy until that
  // abandoned request's `settle` resolves, not start fresh as idle while
  // secretly blocked from firing by `draftInFlight` below. Gated to `add`
  // mode only (one more #654 P2): edit mode renders no draft panel at all —
  // its Save never depended on `draftInFlight` — so inheriting `drafting`
  // there would disable Save with no visible cause and no way to clear it.
  useEffect(() => {
    if (mode !== "add" || draftInFlight === null) return;
    setDrafting(true);
    void draftInFlight.settle.then(() => {
      if (mountedRef.current) setDrafting(false);
    });
  }, [mode]);

  // Editing a field orphans any provenance/evidence it carried (#627): those
  // describe the value the SERVER extracted, not whatever the operator just
  // typed — `withFieldEdited` drops the stale entry so it is never re-attributed
  // to the new value. It ALSO invalidates an in-flight draft's DATA (#654
  // round 2 fold 3): bumping the token here makes the eventual response's own
  // staleness check drop it.
  //
  // Invalidation is TOKEN-BUMP ONLY (#654 verdict round) — it deliberately
  // does NOT abort the fetch or touch the single-flight guard (`draftInFlight`
  // module state). An earlier version aborted the request, but
  // `AbortController.abort()` settles the fetch's promise IMMEDIATELY on the
  // client, while the backend has no disconnect check — so that abort was
  // defeating its own purpose: the guard would release (via
  // `handleDraftFromUrl`'s `finally`, which runs on that immediate abort-
  // rejection) long before the backend's one-at-a-time admission slot was
  // actually free, letting a fresh attempt fire straight into a self-
  // inflicted 429 anyway. Without an abort, the guard genuinely holds until
  // the ORIGINAL response arrives — the only signal the client has for
  // "the backend is done with this one" — which is what makes the hold
  // correct. A true server-side cancel would need a disconnect check on the
  // backend; out of scope, and unnecessary for correctness once the guard
  // holds to the real response.
  const invalidateInFlightDraft = () => {
    if (!drafting) return;
    draftRequestIdRef.current += 1;
  };
  const onChange = (field: keyof BeanProfileDraft, value: string) => {
    invalidateInFlightDraft();
    setDraft((d) => withFieldEdited(d, field, value));
    if (SCOUTING_NOTE_SUMMARIZED_FIELDS.has(field)) setScoutingNote(null);
  };
  const onBlendChange = (checked: boolean) => {
    invalidateInFlightDraft();
    setDraft((d) => withFieldEdited(d, "is_blend", checked));
    // Clearing the field's own validation error on the explicit choice itself
    // (#654 final round), not only at the next save attempt: the operator
    // just resolved exactly what the error was blocking on, so leaving the
    // stale error visible until they hit Save would be a confusing lag.
    setErrors((e) => {
      if (e.is_blend === undefined) return e;
      const { is_blend: _removed, ...rest } = e;
      return rest;
    });
  };

  const title = mode === "add" ? "Add Bean Profile" : "Edit Bean Profile";

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    // Guarded here (not just via the disabled Save button, #637): pressing Enter
    // with focus in any OTHER form field submits natively regardless of the
    // button's disabled attribute, so a draft in flight must block at the
    // handler too — the simplest-correct pairing with the request-token guard
    // in `handleDraftFromUrl` (latest-token-wins + Save blocked meanwhile).
    if (submitting || drafting) return;
    setSubmitError(null);

    const result = validateBeanProfile(draft);
    if ("errors" in result) {
      setErrors(result.errors);
      return;
    }
    setErrors({});
    setSubmitting(true);
    try {
      const saved = await onSave(result.input, draftAttemptId);
      onSaved(saved);
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 409 && draftAttemptId !== undefined) {
          setDraftAttemptId(undefined);
          setSubmitError(
            `${err.detail || "The drafted save link is no longer valid."} Review the fields, then Save again to create this profile manually.`,
          );
        } else {
          setSubmitError(err.detail || `Request failed (${err.status}).`);
        }
      } else {
        setSubmitError(err instanceof Error ? err.message : "Request failed.");
      }
    } finally {
      setSubmitting(false);
    }
  };

  const handleArchive = async () => {
    if (submitting || onArchive === undefined || profile === undefined) return;
    setSubmitError(null);
    setSubmitting(true);
    try {
      await onArchive(profile.id);
      onClose();
    } catch (err) {
      if (err instanceof ApiError) {
        setSubmitError(err.detail || `Request failed (${err.status}).`);
      } else {
        setSubmitError(err instanceof Error ? err.message : "Request failed.");
      }
    } finally {
      setSubmitting(false);
    }
  };

  const handleDraftFromUrl = async () => {
    const url = draftUrl.trim();
    // Synchronous check-and-set against the MODULE-scope guard (#654 final
    // thread) — see `draftInFlight` above. Also refused while a save is in
    // flight (#654 landing round): the modal may unmount on a successful
    // save, and no vendor/LLM call should ever start mid-save regardless.
    if (draftInFlight !== null || submitting || url === "") return;
    const requestId = ++draftRequestIdRef.current;
    let resolveSettle!: () => void;
    draftInFlight = {
      settle: new Promise<void>((resolve) => {
        resolveSettle = resolve;
      }),
    };
    setDrafting(true);
    setDraftErrorKind(null);
    setDraftErrorDetail(null);
    try {
      const response = await api.draftBeanFromUrl(url);
      // Superseded (#637, #654 round 2), or this instance unmounted while the
      // request was in flight (#654 final thread) — either way, never apply a
      // stale response's DATA, and never setState on an unmounted instance.
      if (draftRequestIdRef.current !== requestId || !mountedRef.current) return;
      const { draft: seeded, scoutingNote: note } = draftFromBeanProfileDraft(response);
      setDraft(seeded);
      setErrors({});
      // A stale error from a PREVIOUS failed save must not caption a freshly
      // seeded draft (#654 landing round, cheap fix).
      setSubmitError(null);
      // The note is paired with the draft it describes (#637): only replaced by
      // a NEW successful response, never cleared pre-emptively on the next
      // attempt — a failed retry must not erase the still-active prior draft's
      // explanation.
      setScoutingNote(note);
      setDraftAttemptId(response.draft_attempt_id);
    } catch (err) {
      if (draftRequestIdRef.current !== requestId || !mountedRef.current) return;
      if (err instanceof ApiError && err.status === 422) {
        setDraftErrorKind("invalid");
        setDraftErrorDetail(err.detail);
      } else if (err instanceof ApiError && err.status === 503) {
        setDraftErrorKind("unavailable");
        setDraftErrorDetail(err.detail);
      } else if (err instanceof ApiError) {
        setDraftErrorKind("other");
        setDraftErrorDetail(err.detail || `Request failed (${err.status}).`);
      } else {
        setDraftErrorKind("other");
        setDraftErrorDetail(err instanceof Error ? err.message : "Request failed.");
      }
    } finally {
      // The MODULE guard clears UNCONDITIONALLY on settle (#654 final
      // thread), regardless of token match or mount state — a remounted (or
      // still-mounted-but-superseded) instance can only safely fire once the
      // real backend admission slot is free, which this settle is the sole
      // signal for. `drafting` (React state) stays UNCONDITIONAL on token
      // match too (#654 landing round) — even a superseded request must
      // release Save once it settles; only mount state gates it, since a
      // setState on an unmounted instance is the one thing to avoid here.
      draftInFlight = null;
      resolveSettle();
      if (mountedRef.current) {
        setDrafting(false);
      }
    }
  };

  return (
    <div
      data-testid="bean-profile-modal"
      role="dialog"
      aria-modal="true"
      aria-labelledby="bean-profile-modal-title"
      className="fixed inset-0 z-50 flex items-center justify-center bg-background/70 p-6"
    >
      <form
        onSubmit={(e) => void handleSubmit(e)}
        noValidate
        data-testid="bean-profile-form"
        className="flex max-h-full w-full max-w-2xl flex-col gap-5 overflow-auto rounded-lg border border-border bg-card p-6 shadow-2xl"
      >
        <header className="flex items-start justify-between gap-4">
          <h2
            id="bean-profile-modal-title"
            className="text-lg font-bold uppercase tracking-wide"
          >
            {title}
          </h2>
          <button
            type="button"
            onClick={onClose}
            data-testid="bean-profile-cancel"
            aria-label="Close"
            className="rounded-md border border-input px-3 py-1 text-sm text-muted-foreground transition-colors hover:bg-muted/40"
          >
            Cancel
          </button>
        </header>

        {mode === "edit" && (
          <p className="rounded-md border border-border bg-muted/30 px-4 py-2 text-xs text-muted-foreground">
            Edits affect future roasts only. Past roasts keep their saved settings.
          </p>
        )}

        {mode === "add" && (
          <div
            data-testid="bean-profile-draft-panel"
            className="flex flex-col gap-2 rounded-md border border-border bg-muted/20 p-4"
          >
            <label
              htmlFor="bean-profile-draft-url"
              className="text-xs font-semibold uppercase tracking-wide text-muted-foreground"
            >
              Draft from a vendor page (optional)
            </label>
            {/* Stacks below `sm` (input full-width, button underneath) — consistent
                with the rest of the form's single-column collapse (#654 final
                round). The scoped Playwright baseline is captured at the desktop
                1600px viewport, well above `sm`, so this never touches its pixels. */}
            <div className="flex flex-col gap-2 sm:flex-row">
              <input
                id="bean-profile-draft-url"
                type="url"
                data-testid="bean-profile-draft-url"
                value={draftUrl}
                onChange={(e) => {
                  // #654 final round: editing the URL mid-flight invalidates the
                  // in-flight request the SAME way a profile-field edit does —
                  // without this, a fresh Enter on the NEW url could still get
                  // clobbered by a stale response seeded from the OLD url.
                  invalidateInFlightDraft();
                  setDraftUrl(e.target.value);
                }}
                onKeyDown={(e) => {
                  // #637: Enter in a text input implicitly submits its enclosing
                  // <form> — without this, pressing Enter here would SAVE the
                  // profile (whatever is currently filled) instead of drafting
                  // from the URL just typed. Prevent the native submission and
                  // route Enter to the draft action instead, respecting the same
                  // disabled/empty guard as the button.
                  if (e.key !== "Enter") return;
                  e.preventDefault();
                  void handleDraftFromUrl();
                }}
                placeholder="https://roaster.example.com/the-bean"
                className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm outline-none transition-colors focus:ring-1 focus:ring-ring sm:flex-1"
              />
              <button
                type="button"
                onClick={() => void handleDraftFromUrl()}
                disabled={drafting || submitting || draftUrl.trim() === ""}
                aria-disabled={drafting || submitting || draftUrl.trim() === ""}
                data-testid="bean-profile-draft-button"
                className={cn(
                  "w-full shrink-0 rounded-md border border-roast-coffee/60 bg-roast-coffee/20 px-4 py-2 text-sm font-semibold uppercase tracking-wide text-roast-coffee transition-colors sm:w-auto",
                  drafting || submitting || draftUrl.trim() === ""
                    ? "cursor-not-allowed opacity-60"
                    : "hover:bg-roast-coffee/30",
                )}
              >
                {drafting ? "Drafting…" : "Draft from page"}
              </button>
            </div>
            <span className="text-xs text-muted-foreground">
              Fetches the page and drafts a conservative first-roast profile below for
              you to review — nothing is saved until you submit.
            </span>
            {draftErrorKind !== null &&
              (() => {
                // #654 round 2 fold 4: some backend 422 detail messages embed the
                // requested URL verbatim (e.g. "...validation for '<url>': ...") —
                // a signed/token-bearing query string on that URL must never
                // render on screen. Redacted at the render boundary, not when the
                // state is set, matching the same defense-in-depth-at-display
                // convention `stripBidiControls` uses for evidence quotes.
                const safeDetail =
                  draftErrorDetail !== null ? redactUrlQueryStrings(draftErrorDetail) : null;
                return (
                  <p
                    data-testid="bean-profile-draft-error"
                    role="alert"
                    className="text-xs text-roast-fault"
                  >
                    {draftErrorKind === "invalid" &&
                      `Couldn't draft from that page — check the URL, or the page may be too thin to draft from. ${safeDetail ?? ""}`}
                    {draftErrorKind === "unavailable" &&
                      "Drafting is temporarily unavailable (provider error) — try again in a moment."}
                    {draftErrorKind === "other" && (safeDetail ?? "Request failed.")}
                  </p>
                );
              })()}
            {scoutingNote !== null && (
              <p
                data-testid="bean-profile-scouting-note"
                className="text-xs text-roast-caution"
              >
                {scoutingNote}
              </p>
            )}
          </div>
        )}

        <BeanProfileFields
          draft={draft}
          errors={errors}
          onChange={onChange}
          onBlendChange={onBlendChange}
          testIdPrefix="bean-profile"
          showDefaultWeight
        />

        {submitError !== null && (
          <p
            data-testid="bean-profile-error"
            role="alert"
            className="rounded-md border border-roast-fault/50 bg-roast-fault/10 px-4 py-3 text-sm text-roast-fault"
          >
            {submitError}
          </p>
        )}

        <div className="flex items-center justify-between gap-3">
          {mode === "edit" && onArchive !== undefined ? (
            <button
              type="button"
              onClick={() => void handleArchive()}
              disabled={submitting}
              data-testid="bean-profile-archive"
              className={cn(
                "inline-flex items-center justify-center rounded-md border border-roast-fault/50 px-4 py-2 text-sm font-semibold uppercase tracking-wide text-roast-fault transition-colors",
                submitting ? "cursor-not-allowed opacity-60" : "hover:bg-roast-fault/10",
              )}
            >
              Archive
            </button>
          ) : (
            <span />
          )}
          <button
            type="submit"
            disabled={submitting || drafting}
            aria-disabled={submitting || drafting}
            data-testid="bean-profile-save"
            className={cn(
              "inline-flex items-center justify-center rounded-md border border-roast-coffee/60 bg-roast-coffee/20 px-6 py-2 text-sm font-semibold uppercase tracking-wide text-roast-coffee transition-colors",
              submitting || drafting ? "cursor-not-allowed opacity-60" : "hover:bg-roast-coffee/30",
            )}
          >
            {submitting ? "Saving…" : "Save Profile"}
          </button>
        </div>
      </form>
    </div>
  );
}
