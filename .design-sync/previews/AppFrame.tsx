// Authored preview for AppFrame — the shared app chrome (header brand +
// connection slot) wrapping a routed page. Composed here with a live
// ConnectionIndicator in the header slot and a realistic device-console body.
import { AppFrame, ConnectionIndicator, VerdictBadge } from "roastpilot-web";

function Tile({ label, value }: { label: string; value: string }) {
  return (
    <div
      style={{
        background: "var(--card)",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius)",
        padding: "14px 16px",
        display: "flex",
        flexDirection: "column",
        gap: 6,
      }}
    >
      <span style={{ fontSize: 12, color: "var(--muted-foreground)" }}>
        {label}
      </span>
      <span
        style={{
          fontSize: 24,
          fontWeight: 600,
          fontVariantNumeric: "tabular-nums",
        }}
      >
        {value}
      </span>
    </div>
  );
}

export function DeviceConsole() {
  return (
    <AppFrame headerRight={<ConnectionIndicator status="live" />}>
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 12,
            fontFamily: "ui-sans-serif, system-ui, sans-serif",
          }}
        >
          <span style={{ fontSize: 15, fontWeight: 600 }}>Development</span>
          <VerdictBadge verdict="allow" />
        </div>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(4, 1fr)",
            gap: 12,
            fontFamily: "ui-sans-serif, system-ui, sans-serif",
          }}
        >
          <Tile label="Bean °C" value="198.4" />
          <Tile label="Env °C" value="211.0" />
          <Tile label="Heat %" value="60" />
          <Tile label="Fan %" value="45" />
        </div>
      </div>
    </AppFrame>
  );
}
