// Authored preview for VerdictBadge — RoastPilot advisory safety verdicts.
// The badge renders only allow / clamp / reject (the three advisory tones);
// recovery / fault / emergency_stop deliberately render nothing.
import { VerdictBadge } from "roastpilot-web";

function Surface({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        background: "var(--background)",
        color: "var(--foreground)",
        padding: 24,
        display: "flex",
        gap: 12,
        alignItems: "center",
        fontFamily: "ui-sans-serif, system-ui, sans-serif",
      }}
    >
      {children}
    </div>
  );
}

export function Allow() {
  return (
    <Surface>
      <VerdictBadge verdict="allow" />
    </Surface>
  );
}

export function Clamp() {
  return (
    <Surface>
      <VerdictBadge verdict="clamp" />
    </Surface>
  );
}

export function Reject() {
  return (
    <Surface>
      <VerdictBadge verdict="reject" />
    </Surface>
  );
}

export function AllVerdicts() {
  return (
    <Surface>
      <VerdictBadge verdict="allow" />
      <VerdictBadge verdict="clamp" />
      <VerdictBadge verdict="reject" />
    </Surface>
  );
}
