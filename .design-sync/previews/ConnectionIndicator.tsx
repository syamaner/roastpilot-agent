// Authored preview for ConnectionIndicator — SSE stream liveness in the header.
// connecting / live / reconnecting (pulsing) / stale, each a dot + label tinted
// by the roast safety palette.
import { ConnectionIndicator } from "roastpilot-web";

function Surface({
  children,
  gap = 12,
}: {
  children: React.ReactNode;
  gap?: number;
}) {
  return (
    <div
      style={{
        background: "var(--background)",
        color: "var(--foreground)",
        padding: 24,
        display: "flex",
        gap,
        alignItems: "center",
        fontFamily: "ui-sans-serif, system-ui, sans-serif",
      }}
    >
      {children}
    </div>
  );
}

export function Live() {
  return (
    <Surface>
      <ConnectionIndicator status="live" />
    </Surface>
  );
}

export function Reconnecting() {
  return (
    <Surface>
      <ConnectionIndicator status="reconnecting" />
    </Surface>
  );
}

export function Stale() {
  return (
    <Surface>
      <ConnectionIndicator status="stale" />
    </Surface>
  );
}

export function AllStates() {
  return (
    <Surface gap={24}>
      <ConnectionIndicator status="connecting" />
      <ConnectionIndicator status="live" />
      <ConnectionIndicator status="reconnecting" />
      <ConnectionIndicator status="stale" />
    </Surface>
  );
}
