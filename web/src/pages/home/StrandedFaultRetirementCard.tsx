/** Fixed-copy operator attestation controls for retiring a stranded fault. */

import { useRef, useState } from "react";

import type { StrandedFaultRetirementOutcome } from "@/hooks/useStrandedFaultRetirementRequest";

/** Collect four fresh physical attestations before handing the gesture to the page. */
export function StrandedFaultRetirementCard({ onSubmit }: { onSubmit: () => void }): React.JSX.Element {
  const [open, setOpen] = useState(false);
  const [ticks, setTicks] = useState([false, false, false, false]);
  const consumedRef = useRef(false);
  const allTicked = ticks.every(Boolean);
  const reset = (): void => setTicks([false, false, false, false]);
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
    <section data-testid="stranded-fault-retirement-card" className="flex flex-col gap-4 rounded-lg border border-roast-fault/50 bg-roast-fault/10 p-5">
      <h2 className="text-lg font-bold">Confirm stopped roaster and retire stranded fault</h2>
      <p>The Agent can only retire the stranded fault record. This does not inspect the Hottop, prove its physical state, or turn heat, fan or cooling on. Starting stays blocked, and a separate new restart clearance is required afterwards.</p>
      <p data-testid="stranded-fault-physical-stop-operative">The independent physical emergency stop is the operative stop. A software emergency stop for the stranded session cannot be confirmed.</p>
      {!open ? <button type="button" data-testid="stranded-fault-open" onClick={openCard}>Confirm physical checks</button> : <>
        {labels.map(([id, label], index) => <label key={id}><input data-testid={id} type="checkbox" checked={ticks[index]} onChange={() => setTicks((current) => current.map((tick, i) => i === index ? !tick : tick))} /> {label}</label>)}
        <div className="flex gap-2"><button type="button" data-testid="stranded-fault-cancel" onClick={cancel}>Cancel</button><button type="button" data-testid="stranded-fault-submit" disabled={!allTicked} onClick={submit}>Retire stranded fault</button></div>
      </>}
    </section>
  );
}

/** Explain that the page already has one submitted request. */
export function StrandedFaultRetirementPendingNotice(): React.JSX.Element {
  return <p data-testid="stranded-fault-retirement-pending">A stranded-fault retirement request from this page is still pending. Starting remains blocked. The independent physical emergency stop is the operative stop.</p>;
}

/** Fixed, non-diagnostic result copy for a retirement attempt. */
export function StrandedFaultRetirementOutcomeNotice({ kind, onRefreshStatus }: { kind: StrandedFaultRetirementOutcome; onRefreshStatus: () => void }): React.JSX.Element {
  const messages: Record<StrandedFaultRetirementOutcome, string> = {
    confirmed: "Stranded fault record retired by your attestation. This is not proof that the roaster is in a safe state. Starting remains blocked until you complete a new, separate restart clearance with a fresh physical confirmation.",
    unconfirmed: "Retirement is unconfirmed. Starting remains blocked. Check refreshed status before any further action.",
    rejected: "Retirement was rejected. Check current status.",
    error: "Retirement result could not be confirmed. Check current status.",
  };
  return <div data-testid="stranded-fault-retirement-outcome"><p>{messages[kind]}</p>{kind === "confirmed" && <button type="button" data-testid="stranded-fault-refresh-status" onClick={onRefreshStatus}>Refresh current status</button>}</div>;
}
