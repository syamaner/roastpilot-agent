import type { Config } from "tailwindcss";

// Tokens are declared as CSS custom properties in src/styles/tokens.css and
// surfaced here as Tailwind colors. Dark is the only M1 theme, so the values
// live unconditionally on :root (NOT in a `.dark {}` block — the sketch
// export's trap). `hsl(var(--x))` is not used: the roast palette is authored
// as hex/oklch literals, so we reference the vars directly.
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        background: "var(--background)",
        foreground: "var(--foreground)",
        card: {
          DEFAULT: "var(--card)",
          foreground: "var(--card-foreground)",
        },
        popover: {
          DEFAULT: "var(--popover)",
          foreground: "var(--popover-foreground)",
        },
        primary: {
          DEFAULT: "var(--primary)",
          foreground: "var(--primary-foreground)",
        },
        secondary: {
          DEFAULT: "var(--secondary)",
          foreground: "var(--secondary-foreground)",
        },
        muted: {
          DEFAULT: "var(--muted)",
          foreground: "var(--muted-foreground)",
        },
        accent: {
          DEFAULT: "var(--accent)",
          foreground: "var(--accent-foreground)",
        },
        destructive: {
          DEFAULT: "var(--destructive)",
          foreground: "var(--destructive-foreground)",
        },
        border: "var(--border)",
        input: "var(--input)",
        ring: "var(--ring)",
        // RoastPilot domain palette (component plan §7 / ui-prompts.md):
        // amber/copper = heat, teal = fan/air; nominal/caution/fault map to
        // the verdict + safety colors; phase-* shifts with the roast.
        roast: {
          heat: "var(--roast-heat)",
          "heat-dim": "var(--roast-heat-dim)",
          fan: "var(--roast-fan)",
          "fan-dim": "var(--roast-fan-dim)",
          coffee: "var(--roast-coffee)",
          nominal: "var(--roast-nominal)",
          caution: "var(--roast-caution)",
          fault: "var(--roast-fault)",
          "phase-preheat": "var(--roast-phase-preheat)",
          "phase-roasting": "var(--roast-phase-roasting)",
          "phase-development": "var(--roast-phase-development)",
          "phase-cooling": "var(--roast-phase-cooling)",
        },
      },
      borderRadius: {
        lg: "var(--radius)",
        md: "calc(var(--radius) - 2px)",
        sm: "calc(var(--radius) - 4px)",
      },
      fontFamily: {
        // Tabular figures for every numeric display (temps/timers/percentages)
        // — readable from 1 m at the roaster (kickoff §1, ui-prompts shared block).
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "Consolas", "monospace"],
      },
    },
  },
  plugins: [],
} satisfies Config;
