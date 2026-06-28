# RoastPilot design system — conventions

RoastPilot is a **dark-only** device console for a coffee roaster. All temperatures
are Celsius. Components are real React, imported from `window.RoastPilotDS.*`.

## Setup & wrapping

- **No provider/wrapper is required.** All design tokens are plain CSS custom
  properties defined on `:root` and shipped in `styles.css`'s import closure
  (`_ds_bundle.css`), so any component is styled as soon as the design includes
  `styles.css`.
- The theme is **dark, unconditional** — tokens live on `:root`, there is no
  `.dark` class and no light theme. Put content on a dark surface: set the page
  / container background to `var(--background)` and text to `var(--foreground)`.
  `AppFrame` already does this for you (it renders `min-h-screen bg-background`).
- **`LiveCurve`** reads the `--roast-*` / `--muted-foreground` / `--border`
  custom properties at mount to colour its uPlot canvas, and ships uPlot's own
  base CSS inside `_ds_bundle.css` — both arrive automatically via `styles.css`.
  Give it real `points` (an empty array renders an empty chart).

## Styling idiom

Tailwind utility classes compiled against CSS-custom-property tokens. The
compiled utilities the components use are baked into `_ds_bundle.css`. For your
own layout glue, prefer the **CSS variables directly** (reliable regardless of
the Tailwind content scan) or the matching utility names:

| Token (use as `var(--…)` or the utility) | Meaning |
|---|---|
| `--background` / `bg-background`, `--foreground` / `text-foreground` | page surface + text |
| `--card` / `bg-card`, `--border` / `border-border` | panel surface + hairline |
| `--muted-foreground` / `text-muted-foreground` | secondary/label text |
| `--primary`, `--secondary`, `--accent`, `--destructive`, `--ring` | base palette |
| `--radius` | corner radius (cards, badges) |
| `--roast-heat` (amber), `--roast-fan` (teal), `--roast-coffee` | control / bean colours |
| `--roast-nominal` (green), `--roast-caution` (amber), `--roast-fault` (red) | verdict / safety tones |
| `--roast-phase-preheat / -roasting / -development / -cooling` | phase accents |

Numeric readouts (temps, timers, %) use tabular figures — add the `.numeric`
class or `font-variant-numeric: tabular-nums`.

## Where the truth lives

- `_ds_bundle.css` (reached from `styles.css`) — every token definition + the
  compiled utilities. Read it before inventing styles.
- Per component: `<Name>.d.ts` (the props contract) and `<Name>.prompt.md` (usage).

## Idiomatic example

```tsx
const { AppFrame, ConnectionIndicator, VerdictBadge } = window.RoastPilotDS;

<AppFrame headerRight={<ConnectionIndicator status="live" />}>
  <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
    <span style={{ fontSize: 15, fontWeight: 600 }}>Development</span>
    <VerdictBadge verdict="allow" />
  </div>
  <div
    style={{
      background: "var(--card)",
      border: "1px solid var(--border)",
      borderRadius: "var(--radius)",
      padding: "14px 16px",
    }}
  >
    <span style={{ color: "var(--muted-foreground)", fontSize: 12 }}>Bean °C</span>
    <div style={{ fontSize: 24, fontWeight: 600, fontVariantNumeric: "tabular-nums" }}>
      198.4
    </div>
  </div>
</AppFrame>
```
