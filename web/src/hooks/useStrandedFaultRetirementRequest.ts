/** Page-owned request state for the stranded-fault retirement endpoint. */

import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { roastKeys } from "@/hooks/queries";
import { api } from "@/lib/api";

export type StrandedFaultRetirementOutcome = "confirmed" | "unconfirmed" | "rejected" | "error";

/** Submit one retirement request at most once at a time for this page lifetime. */
export function useStrandedFaultRetirementRequest(): {
  pending: boolean;
  outcome: { kind: StrandedFaultRetirementOutcome; key: string } | null;
  submit: (key: string) => void;
} {
  const queryClient = useQueryClient();
  const inFlightRef = useRef(false);
  const mountedRef = useRef(true);
  const [pending, setPending] = useState(false);
  const [outcome, setOutcome] = useState<{ kind: StrandedFaultRetirementOutcome; key: string } | null>(null);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const submit = (key: string): void => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    setPending(true);
    setOutcome(null);
    void api
      .retireStrandedFault()
      .then((result) => result.outcome)
      .catch((): StrandedFaultRetirementOutcome => "error")
      .then((kind) => {
        if (mountedRef.current) setOutcome({ kind, key });
      })
      .finally(() => {
        inFlightRef.current = false;
        if (mountedRef.current) setPending(false);
        void queryClient.invalidateQueries({ queryKey: roastKeys.health });
      });
  };

  return { pending, outcome, submit };
}
