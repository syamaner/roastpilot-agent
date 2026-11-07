/** Fixed-copy operator attestation controls for retiring a stranded fault. */

import { useEffect, useRef, useState } from "react";

import type { StrandedFaultRetirementOutcome } from "@/hooks/useStrandedFaultRetirementRequest";

/** Collect four fresh physical attestations before handing the gesture to the page. */
export function StrandedFaultRetirementCard({ onSubmit }: { onSubmit: () => void }): React.JSX.Element {
  const [open, setOpen] = useState(false);
  const [ticks, setTicks] = useState([false, false, false, false]);
  const consumedRef = useRef(false);
  const firstCheckRef = useRef<HTMLInputElement>(null);
  const allTicked = ticks.every(Boolean);
  const reset = (): void => setTicks([false, false, false, false]);
  useEffect(() => {
    if (open) firstCheckRef.current?.focus();
  }, [open]);
  const openCard = (): void => {
    consumedRef.current = false;
    reset();
    setOpen(true);
  };
  const cancel = (): void => {
    reset();
    setOpen(false);
  };
  const submit = (): void => {
    if (!allTicked || consumedRef.current) return;
    consumedRef.current = true;
    reset();
    setOpen(false);
    onSubmit();
  };
  const labels = [
    ["stranded-fault-stopped-empty", "I physically checked that the Hottop is stopped and empty."],
    ["stranded-fault-heat-off", "The heater is off."],
    ["stranded-fault-fan-cooling-off", "The fan and cooling are off."],
    ["stranded-fault-independent-stop", "The independent physical emergency stop is engaged or immediately ready."],
  ] as const;
  return (
    <section data-testid="stranded-fault-retirement-card" className="mx-auto flex max-w-xl flex-col items-center gap-4 rounded-lg border border-roast-fault/50 bg-roast-fault/10 p-8 text-center">
      <div className="flex flex-col gap-3">
        <h2 className="text-lg font-bold uppercase tracking-wide">Confirm stopped roaster and retire stranded fault</h2>
        <p className="text-sm text-muted-foreground">The Agent can only retire the stranded fault record. This does not inspect the Hottop, prove its physical state, or turn heat, fan or cooling on. Starting stays blocked, and a separate new restart clearance is required afterwards.</p>
      </div>
      <p data-testid="stranded-fault-physical-stop-operative" className="text-xs text-muted-foreground">The independent physical emergency stop is the operative stop. A software emergency stop for the stranded session cannot be confirmed.</p>
      {!open ? <button type="button" data-testid="stranded-fault-open" onClick={openCard} className="rounded-md border border-roast-fault/50 px-5 py-3 text-sm font-semibold text-roast-fault transition-colors hover:bg-roast-fault/10">Confirm physical checks</button> : <div className="flex w-full flex-col gap-3 text-left">
        {labels.map(([id, label], index) => <label key={id} className="flex items-start gap-3 text-sm text-foreground"><input ref={index === 0 ? firstCheckRef : undefined} data-testid={id} type="checkbox" checked={ticks[index]} onChange={() => setTicks((current) => current.map((tick, i) => i === index ? !tick : tick))} className="mt-0.5 h-4 w-4" /> <span>{label}</span></label>)}
        <div className="flex flex-wrap items-center justify-center gap-2 pt-1"><button type="button" data-testid="stranded-fault-submit" disabled={!allTicked} onClick={submit} className="rounded-md bg-roast-fault px-4 py-2 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-50">Retire stranded fault</button><button type="button" data-testid="stranded-fault-cancel" onClick={cancel} className="rounded-md border border-border px-4 py-2 text-sm font-medium text-foreground">Cancel</button></div>
      </div>}
    </section>
  );
}

/** Explain that the page already has one submitted request. */
export function StrandedFaultRetirementPendingNotice(): React.JSX.Element {
  return <div data-testid="stranded-fault-retirement-pending" role="status" aria-live="polite" aria-atomic="true" className="mx-auto flex max-w-xl flex-col gap-3 rounded-lg border border-roast-fault/50 bg-roast-fault/10 p-5 text-center text-sm text-muted-foreground"><p>A stranded-fault retirement request from this page is still pending. Starting remains blocked. The independent physical emergency stop is the operative stop.</p></div>;
}

/** Fixed, non-diagnostic result copy for a retirement attempt. */
export function StrandedFaultRetirementOutcomeNotice({ kind, onRefreshStatus }: { kind: StrandedFaultRetirementOutcome; onRefreshStatus: () => void }): React.JSX.Element {
  const messages: Record<StrandedFaultRetirementOutcome, string> = {
    confirmed: "Stranded fault record retired by your attestation. This is not proof that the roaster is in a safe state. Starting remains blocked until you complete a new, separate restart clearance with a fresh physical confirmation.",
    unconfirmed: "Retirement is unconfirmed. Starting remains blocked. Check refreshed status before any further action.",
    rejected: "Retirement was rejected. Check current status.",
    error: "Retirement result could not be confirmed. Check current status.",
  };
  return <div data-testid="stranded-fault-retirement-outcome" role="status" aria-live="polite" aria-atomic="true" className="mx-auto flex max-w-xl flex-col items-center gap-3 rounded-lg border border-border bg-card p-5 text-center"><p className="text-sm text-muted-foreground">{messages[kind]}</p>{kind === "confirmed" && <button type="button" data-testid="stranded-fault-refresh-status" onClick={onRefreshStatus} className="rounded-md border border-border px-4 py-2 text-sm font-medium text-foreground">Refresh current status</button>}</div>;
}
