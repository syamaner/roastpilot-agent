// Authored preview for LiveCurve — the shared five-series roast chart (bean/env
// °C, RoR °C/min, heat/fan %). Data is a synthesized but realistic Hottop roast:
// charge dive → turning point → exponential rise through first crack to the drop,
// with T0 / dry-end / first-crack / drop markers.
import { LiveCurve } from "roastpilot-web";

interface Pt {
  t: number;
  bean: number | null;
  env: number | null;
  ror: number | null;
  heat: number | null;
  fan: number | null;
}

const CHARGE = 45; // serve seconds at charge / T0
const STEP = 15;
const DROP_TAU = 690;

function beanAt(tau: number): number {
  if (tau <= 75) return 190 - (190 - 85) * (tau / 75); // dive to turning point
  return 85 + (196 - 85) * (1 - Math.exp(-(tau - 75) / 250)); // exp rise
}
function envAt(tau: number): number {
  if (tau <= 60) return 205 - (205 - 168) * (tau / 60);
  return 168 + (214 - 168) * (1 - Math.exp(-(tau - 60) / 220));
}
function heatAt(tau: number): number {
  if (tau < 200) return 100;
  if (tau < 400) return 80;
  if (tau < 560) return 60;
  if (tau < DROP_TAU) return 40;
  return 0;
}
function fanAt(tau: number): number {
  if (tau < 300) return 30;
  if (tau < 560) return 40;
  return 60;
}

function buildRoast(): Pt[] {
  const pts: Pt[] = [];
  // Preheat: empty drum warming, no roast clock yet.
  for (let t = 0; t < CHARGE; t += STEP) {
    pts.push({ t, bean: 188, env: 205, ror: null, heat: 100, fan: 30 });
  }
  let prevBean: number | null = null;
  for (let tau = 0; tau <= DROP_TAU; tau += STEP) {
    const bean = beanAt(tau);
    // RoR (°C/min) from the finite difference, surfaced only once past the
    // turning-point swing — the live UI reads the declining post-TP RoR, not
    // the charge crater / TP spike.
    let ror: number | null = null;
    if (tau >= 120 && prevBean !== null) {
      ror = Math.round(((bean - prevBean) / (STEP / 60)) * 10) / 10;
    }
    pts.push({
      t: CHARGE + tau,
      bean: Math.round(bean * 10) / 10,
      env: Math.round(envAt(tau) * 10) / 10,
      ror,
      heat: heatAt(tau),
      fan: fanAt(tau),
    });
    prevBean = bean;
  }
  return pts;
}

const ROAST = buildRoast();

const MARKERS = [
  { kind: "t0" as const, t: CHARGE, label: "T0" },
  { kind: "dry_end" as const, t: CHARGE + 330, label: "DRY END" },
  { kind: "first_crack" as const, t: CHARGE + 560, label: "FC" },
  { kind: "drop" as const, t: CHARGE + DROP_TAU, label: "DROP" },
];

function Surface({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        background: "var(--background)",
        color: "var(--foreground)",
        padding: 16,
        fontFamily: "ui-sans-serif, system-ui, sans-serif",
      }}
    >
      {children}
    </div>
  );
}

export function DevelopedRoast() {
  return (
    <Surface>
      <LiveCurve
        points={ROAST}
        markers={MARKERS}
        originSeconds={CHARGE}
        phase="development"
        height={400}
      />
    </Surface>
  );
}

export function Preheating() {
  const preheat: Pt[] = [];
  for (let t = 0; t <= 60; t += STEP) {
    preheat.push({
      t,
      bean: Math.round((120 + (192 - 120) * (t / 60)) * 10) / 10,
      env: Math.round((150 + (205 - 150) * (t / 60)) * 10) / 10,
      ror: null,
      heat: 100,
      fan: 30,
    });
  }
  return (
    <Surface>
      <LiveCurve
        points={preheat}
        phase="preheating"
        chargeBand={{ minC: 170, maxC: 200 }}
        height={400}
      />
    </Surface>
  );
}
