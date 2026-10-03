/// <reference types="node" />
/**
 * Cold-characterisation view, harness and route tests (#954 U3).
 *
 * V1-V7: rendered copy and values; H5b: the harness source's microtask deferral;
 * H6: no REST, XHR or storage and one constructed source; F1: a literal-path source
 * scan; F2: both routes are top-level, outside the operator layout, and their lazy
 * imports resolve through the real `App` router.
 */

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { act, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "@/App";
import {
  useColdObservationStream,
  type ColdEventSourceLike,
  type ColdObservationStreamState,
} from "@/hooks/useColdObservationStream";
import type { ColdObservation } from "@/lib/coldObservation";
import { routes } from "@/routes";

import { ColdCharacterisationView } from "./ColdCharacterisationPage";
import { HarnessEventSource, type HarnessEmission } from "./ColdCharacterisationHarnessPage";

const PINNED_SENTENCE =
  "These values are the last retained display observation; they are not accepted or safe ticks. Heat, roast fan and cooling are commanded state reported by MCP, not measurements.";
const FORBIDDEN = /\b(live|stale|fresh|ready|qualif\w*|safe|accepted|ongoing)\b/gi;

const OBSERVATION: ColdObservation = {
  schema_version: 1,
  cold_phase: "recording_on",
  recorded_at_utc: "2026-01-01T00:00:01.250000+00:00",
  device_reported: true,
  bean_temp_c: 21.5,
  env_temp_c: 22.5,
  heat_percent: 0,
  fan_percent: null,
  roast_fan_outcome: "observed",
  roast_fan_percent: 40,
  cooling_on: false,
};

function state(overrides: Partial<ColdObservationStreamState> = {}): ColdObservationStreamState {
  return {
    status: "connecting",
    observation: null,
    lastId: null,
    shownCount: 0,
    refusedCount: 0,
    ...overrides,
  };
}

function renderView(overrides: Partial<ColdObservationStreamState> = {}, synthetic = false) {
  return render(<ColdCharacterisationView state={state(overrides)} synthetic={synthetic} />);
}

function text(testId: string): string | null {
  return screen.getByTestId(testId).textContent;
}

/**
 * The rendered document text with element boundaries kept as spaces. Plain
 * `textContent` concatenates adjacent elements ("Frames accepted" + "0" becomes
 * "Frames accepted0"), which would hide a whole word from a `\b` check.
 */
function renderedText(): string {
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const parts: string[] = [];
  for (let node = walker.nextNode(); node !== null; node = walker.nextNode()) {
    parts.push(node.textContent ?? "");
  }
  return parts.join(" ");
}

/** Forbidden whole words left after removing exactly ONE pinned-sentence occurrence. */
function forbiddenWords(documentText: string): string[] {
  const at = documentText.indexOf(PINNED_SENTENCE);
  const remaining =
    at < 0
      ? documentText
      : documentText.slice(0, at) + documentText.slice(at + PINNED_SENTENCE.length);
  return remaining.match(FORBIDDEN) ?? [];
}

/** A scripted fake that records each constructed source for the page-level tests. */
class RecordingSource implements ColdEventSourceLike {
  static instances: RecordingSource[] = [];
  onopen: ColdEventSourceLike["onopen"] = null;
  onerror: ColdEventSourceLike["onerror"] = null;
  readonly listeners = new Map<string, ((ev: MessageEvent) => void)[]>();
  closed = false;

  constructor(readonly url: string) {
    RecordingSource.instances.push(this);
  }

  addEventListener(type: string, listener: (ev: MessageEvent) => void): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }

  close(): void {
    this.closed = true;
  }

  emit(type: string, data: string, lastEventId = ""): void {
    act(() => {
      for (const listener of this.listeners.get(type) ?? []) {
        listener(new MessageEvent(type, { data, lastEventId }));
      }
    });
  }
}

/**
 * A bounded synthetic `fetch`: every call resolves at once to a valid empty JSON
 * 200, so a regression that adds a REST query renders normally and is caught by
 * the call assertion rather than by a crash, a rejection or a timeout.
 */
function syntheticFetch() {
  return vi.fn(
    async () =>
      new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } }),
  );
}

let routeQueryClient: QueryClient | null = null;

/**
 * Render the real `App` router at `path` inside the providers the production entry
 * point supplies (a fresh per-test QueryClient, no retries or refetching) with the
 * synthetic `fetch` installed. Returns the render result and the fetch spy.
 */
function renderRoute(path: string) {
  const fetchSpy = syntheticFetch();
  vi.stubGlobal("fetch", fetchSpy);
  routeQueryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        refetchOnWindowFocus: false,
        refetchOnReconnect: false,
        refetchOnMount: false,
        gcTime: 0,
      },
    },
  });
  const rendered = render(
    <QueryClientProvider client={routeQueryClient}>
      <MemoryRouter initialEntries={[path]}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...rendered, fetchSpy };
}

/** Let any effect-scheduled request start before asserting there was none. */
async function settle(): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

afterEach(() => {
  routeQueryClient?.clear();
  routeQueryClient = null;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  RecordingSource.instances = [];
});

describe("V1 before the first retained observation", () => {
  it("shows formatter null output and no digits in the value grid", () => {
    renderView();
    expect(text("cold-last-utc")).toBe("—");
    expect(text("cold-phase")).toBe("—");
    expect(text("cold-bean")).toBe("— °C");
    expect(text("cold-env")).toBe("— °C");
    expect(text("cold-heat")).toBe("— %");
    expect(text("cold-roast-fan")).toBe("—");
    expect(text("cold-cooling")).toBe("—");
    expect(screen.queryByTestId("cold-no-device")).toBeNull();
    const grid = screen.getByTestId("cold-bean").closest("dl");
    expect(grid?.textContent).not.toMatch(/\d/);
    expect(text("cold-shown-count")).toBe("0");
    expect(text("cold-refused-count")).toBe("0");
  });
});

describe("V2 field rendering", () => {
  it("renders exact strings with commanded labelling", () => {
    renderView({ status: "connected", observation: OBSERVATION, shownCount: 3, refusedCount: 2 });
    expect(text("cold-last-utc")).toBe("2026-01-01T00:00:01.250000+00:00");
    expect(text("cold-phase")).toBe("Recording on");
    expect(text("cold-bean")).toBe("21.5 °C");
    expect(text("cold-env")).toBe("22.5 °C");
    expect(text("cold-heat")).toBe("0 %");
    expect(text("cold-roast-fan")).toBe("Observed, 40 %");
    expect(text("cold-cooling")).toBe("Off");
    expect(text("cold-shown-count")).toBe("3");
    expect(text("cold-refused-count")).toBe("2");
    expect(screen.getByText("Heat (commanded)")).toBeInTheDocument();
    expect(screen.getByText("Roast fan (commanded)")).toBeInTheDocument();
    expect(screen.getByText("Cooling (commanded)")).toBeInTheDocument();
    expect(screen.getByText("Frames shown")).toBeInTheDocument();
    expect(screen.getByText("Frames refused")).toBeInTheDocument();
  });

  it("shows the roast-fan percent only when present, and each outcome label", () => {
    const cases: [ColdObservation["roast_fan_outcome"], number | null, string][] = [
      ["observed", null, "Observed"],
      ["observed", 0, "Observed, 0 %"],
      ["not_eligible", null, "Not eligible"],
      ["unsupported", null, "Unsupported"],
      ["unreadable", null, "Unreadable"],
      ["malformed", null, "Malformed"],
    ];
    for (const [outcome, percent, expected] of cases) {
      const { unmount } = renderView({
        observation: { ...OBSERVATION, roast_fan_outcome: outcome, roast_fan_percent: percent },
      });
      expect(text("cold-roast-fan")).toBe(expected);
      unmount();
    }
  });

  it("shows phase off, cooling on/unknown and null values", () => {
    renderView({
      observation: {
        ...OBSERVATION,
        cold_phase: "recording_off",
        cooling_on: true,
        bean_temp_c: null,
        heat_percent: null,
      },
    });
    expect(text("cold-phase")).toBe("Recording off");
    expect(text("cold-cooling")).toBe("On");
    expect(text("cold-bean")).toBe("— °C");
    expect(text("cold-heat")).toBe("— %");
  });

  it("never renders a main-fan value", () => {
    renderView({ observation: OBSERVATION });
    const body = document.body.textContent ?? "";
    expect(body).not.toMatch(/main fan|fan_percent/i);
    // Every "fan" mention on the page is the roast fan; there is no other fan read-out.
    const fanMentions = body.match(/\w*\s?fan\b/gi) ?? [];
    expect(fanMentions.length).toBeGreaterThan(0);
    expect(fanMentions.every((mention) => /^roast fan$/i.test(mention))).toBe(true);
  });
});

describe("V3 device absent", () => {
  it("states no device state and keeps null values", () => {
    renderView({
      observation: {
        ...OBSERVATION,
        device_reported: false,
        bean_temp_c: null,
        env_temp_c: null,
        heat_percent: null,
        cooling_on: null,
      },
    });
    expect(text("cold-no-device")).toBe("No device state in this observation");
    expect(text("cold-bean")).toBe("— °C");
    expect(text("cold-cooling")).toBe("—");
  });
});

describe("V4/V7 wording", () => {
  const states: [string, Partial<ColdObservationStreamState>, boolean][] = [
    ["connecting", {}, false],
    ["connected", { status: "connected", observation: OBSERVATION }, false],
    ["reconnecting", { status: "reconnecting", observation: OBSERVATION }, false],
    [
      "no device",
      {
        observation: {
          ...OBSERVATION,
          device_reported: false,
          bean_temp_c: null,
          env_temp_c: null,
          heat_percent: null,
          cooling_on: null,
        },
      },
      false,
    ],
    ["synthetic", { status: "connected" }, true],
  ];

  it.each(states)("%s: no forbidden word outside the single pinned sentence", (_l, s, synthetic) => {
    renderView(s, synthetic);
    expect(forbiddenWords(renderedText())).toEqual([]);
  });

  it.each(states)("%s: the pinned sentence exists exactly once, byte for byte", (_l, s, synthetic) => {
    renderView(s, synthetic);
    const nodes = screen.getAllByTestId("cold-disclaimer-retained");
    expect(nodes).toHaveLength(1);
    expect(nodes[0]?.textContent).toBe(PINNED_SENTENCE);
    expect(renderedText().split(PINNED_SENTENCE)).toHaveLength(2);
  });

  it("states the fixed copy", () => {
    renderView();
    expect(screen.getByText(/read-only/i)).toBeInTheDocument();
    expect(screen.getByText(/describes the event connection only/i)).toBeInTheDocument();
    expect(screen.getByText(/independent operator emergency stop is required/i)).toBeInTheDocument();
  });

  it("shows the transport status only as connection wording", () => {
    for (const [status, label] of [
      ["connecting", "Event connection: Connecting"],
      ["connected", "Event connection: Connected"],
      ["reconnecting", "Event connection: Reconnecting"],
    ] as const) {
      const { unmount } = renderView({ status });
      expect(screen.getByTestId("cold-transport")).toHaveAttribute("data-status", status);
      expect(text("cold-transport")).toBe(label);
      unmount();
    }
  });

  it("control: the exemption covers one sentence only (a second copy is caught)", () => {
    expect(forbiddenWords(PINNED_SENTENCE)).toEqual([]);
    expect(forbiddenWords(`${PINNED_SENTENCE} ${PINNED_SENTENCE}`)).toEqual(["accepted", "safe"]);
    expect(forbiddenWords("Event connection: Live")).toEqual(["Live"]);
    expect(forbiddenWords("Frames accepted")).toEqual(["accepted"]);
    // Element boundaries are kept, so a label next to a number is still a whole word.
    render(
      <p>
        <span>Frames accepted</span>
        <span>0</span>
      </p>,
    );
    expect(forbiddenWords(renderedText())).toEqual(["accepted"]);
  });
});

describe("V5 refused content never reaches the DOM or console", () => {
  it("drops canaries from refused frames", () => {
    const consoleSpies = (["log", "info", "warn", "error", "debug"] as const).map((method) =>
      vi.spyOn(console, method),
    );
    vi.stubGlobal("EventSource", RecordingSource);
    renderRoute("/cold-characterisation");
    return waitFor(() => expect(RecordingSource.instances).toHaveLength(1)).then(() => {
      const [source] = RecordingSource.instances;
      if (source === undefined) throw new Error("no source");
      const valid = JSON.stringify({ ...OBSERVATION });
      source.emit("observation", valid, "0123456789abcdef-1");
      source.emit("observation", valid.replace("}", ',"x":"CANARY-EXTRA"}'), "0123456789abcdef-2");
      source.emit(
        "observation",
        valid.replace("2026-01-01T00:00:01.250000+00:00", "CANARY-UTC\u202e"),
        "0123456789abcdef-3",
      );
      source.emit("observation", valid.replace("recording_on", "CANARY-PHASE"), "0123456789abcdef-4");
      source.emit("observation", valid.replace("21.5", '"CANARY-BEAN"'), "0123456789abcdef-1");
      source.emit("observation", valid.replace("}", `,"p":"CANARY-BIG${"x".repeat(4096)}"}`), "0123456789abcdef-5");
      source.emit("heartbeat", '{"CANARY-HEARTBEAT":1}');
      source.emit("observation", valid, "CANARY-ID");
      expect(screen.getByTestId("cold-refused-count").textContent).toBe("7");
      expect(screen.getByTestId("cold-shown-count").textContent).toBe("1");
      expect(document.body.innerHTML).not.toContain("CANARY");
      for (const spy of consoleSpies) expect(spy).not.toHaveBeenCalled();
    });
  });
});

describe("V6 synthetic banner", () => {
  it("appears on the harness only", async () => {
    vi.stubGlobal("EventSource", RecordingSource);
    const product = renderRoute("/cold-characterisation");
    expect(await screen.findByTestId("cold-view")).toBeInTheDocument();
    expect(screen.queryByTestId("cold-synthetic-banner")).toBeNull();
    product.unmount();

    renderRoute("/__cold-characterisation-harness");
    expect(await screen.findByTestId("cold-synthetic-banner")).toHaveTextContent(
      "Synthetic harness data — not an observation from any roaster",
    );
  });
});

describe("H5b harness source microtask deferral", () => {
  const EMISSIONS: HarnessEmission[] = [
    { kind: "open" },
    { kind: "event", type: "heartbeat", data: "{}", lastEventId: "" },
  ];

  it("delivers emissions after listener registration", async () => {
    const source = new HarnessEventSource(EMISSIONS);
    const opened = vi.fn();
    const heartbeat = vi.fn();
    source.onopen = opened;
    source.addEventListener("heartbeat", heartbeat);
    expect(opened).not.toHaveBeenCalled();
    await Promise.resolve();
    expect(opened).toHaveBeenCalledTimes(1);
    expect(heartbeat).toHaveBeenCalledTimes(1);
    expect((heartbeat.mock.calls[0]?.[0] as MessageEvent).data).toBe("{}");
  });

  it("an emission whose name has no listener is ignored", async () => {
    const source = new HarnessEventSource([
      { kind: "event", type: "unregistered", data: "{}", lastEventId: "" },
      { kind: "event", type: "heartbeat", data: "{}", lastEventId: "" },
    ]);
    const heartbeat = vi.fn();
    source.addEventListener("heartbeat", heartbeat);
    await Promise.resolve();
    expect(heartbeat).toHaveBeenCalledTimes(1);
  });

  it("close() before the microtask suppresses every emission", async () => {
    const source = new HarnessEventSource(EMISSIONS);
    const opened = vi.fn();
    const heartbeat = vi.fn();
    source.onopen = opened;
    source.addEventListener("heartbeat", heartbeat);
    source.close();
    await Promise.resolve();
    expect(opened).not.toHaveBeenCalled();
    expect(heartbeat).not.toHaveBeenCalled();
  });

  // Illustrative only: this self-contained class is not the harness code. It shows
  // why the harness defers with a microtask; the behavioural proof is the harness
  // tests above and the synchronous-emission mutant of the real harness source.
  it("illustrative contrast: a source emitting synchronously in its constructor loses the frames", () => {
    class SynchronousSource {
      onopen: (() => void) | null = null;
      readonly listeners: ((ev: MessageEvent) => void)[] = [];
      constructor() {
        this.onopen?.();
        for (const listener of this.listeners) listener(new MessageEvent("heartbeat", { data: "{}" }));
      }
      addEventListener(_type: string, listener: (ev: MessageEvent) => void) {
        this.listeners.push(listener);
      }
    }
    const source = new SynchronousSource();
    const heartbeat = vi.fn();
    source.addEventListener("heartbeat", heartbeat);
    expect(heartbeat).not.toHaveBeenCalled();
  });

  it("the harness route drives the real hook: two shown, one refused, values from frame two", async () => {
    renderRoute("/__cold-characterisation-harness");
    await waitFor(() => expect(text("cold-shown-count")).toBe("2"));
    expect(text("cold-refused-count")).toBe("1");
    expect(text("cold-phase")).toBe("Recording on");
    expect(text("cold-bean")).toBe("21.8 °C");
    expect(text("cold-roast-fan")).toBe("Observed, 40 %");
    expect(text("cold-last-utc")).toBe("2026-01-01T00:00:02.000000+00:00");
    expect(screen.getByTestId("cold-transport")).toHaveAttribute("data-status", "connected");
    expect(document.body.innerHTML).not.toContain("HARNESS-HOSTILE-CANARY");
  });

  it("the awaiting harness state shows an open connection and no observation", async () => {
    renderRoute("/__cold-characterisation-harness?state=awaiting");
    await waitFor(() =>
      expect(screen.getByTestId("cold-transport")).toHaveAttribute("data-status", "connected"),
    );
    expect(text("cold-shown-count")).toBe("0");
    expect(text("cold-refused-count")).toBe("0");
    expect(text("cold-bean")).toBe("— °C");
  });
});

describe("H6 one event source; no REST, XHR or storage", () => {
  it("the product route constructs exactly one source and touches nothing else", async () => {
    const xhrOpen = vi.spyOn(XMLHttpRequest.prototype, "open");
    const storageCalls = (["getItem", "setItem", "removeItem", "clear", "key"] as const).map(
      (method) => vi.spyOn(Storage.prototype, method),
    );
    vi.stubGlobal("EventSource", RecordingSource);
    const { fetchSpy } = renderRoute("/cold-characterisation");
    expect(await screen.findByTestId("cold-view")).toBeInTheDocument();
    expect(RecordingSource.instances.map((source) => source.url)).toEqual([
      "/api/cold-characterisation/events",
    ]);
    const [source] = RecordingSource.instances;
    source?.emit("observation", JSON.stringify(OBSERVATION), "0123456789abcdef-1");
    source?.emit("heartbeat", "{}");
    await settle();
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(xhrOpen).not.toHaveBeenCalled();
    for (const spy of storageCalls) expect(spy).not.toHaveBeenCalled();
    expect(screen.queryByRole("navigation")).toBeNull();
  });

  it("retains only the latest observation, its ID and integer counters", () => {
    const sources: RecordingSource[] = [];
    const { result } = renderHook(() =>
      useColdObservationStream({
        createEventSource: (url) => {
          const source = new RecordingSource(url);
          sources.push(source);
          return source;
        },
      }),
    );
    sources[0]?.emit("observation", JSON.stringify(OBSERVATION), "0123456789abcdef-1");
    sources[0]?.emit(
      "observation",
      JSON.stringify({ ...OBSERVATION, bean_temp_c: 30.5 }),
      "0123456789abcdef-2",
    );
    expect(Object.keys(result.current).sort()).toEqual([
      "lastId",
      "observation",
      "refusedCount",
      "shownCount",
      "status",
    ]);
    expect(result.current.observation?.bean_temp_c).toBe(30.5);
    expect(Number.isInteger(result.current.shownCount)).toBe(true);
    expect(Number.isInteger(result.current.refusedCount)).toBe(true);
  });
});

describe("F1 literal-path source scan", () => {
  const HERE = dirname(fileURLToPath(import.meta.url));
  const PRODUCTION_FILES = [
    resolve(HERE, "../../lib/coldObservation.ts"),
    resolve(HERE, "../../hooks/useColdObservationStream.ts"),
    resolve(HERE, "ColdCharacterisationPage.tsx"),
    resolve(HERE, "ColdCharacterisationHarnessPage.tsx"),
  ];
  // Each literal is assembled from two parts so this scanner's own source does not
  // match the repository-level ripgrep sweep over the cold directory.
  const FORBIDDEN_LITERALS = [
    ["@/lib/", "api"],
    ["@/hooks/", "queries"],
    ["useRoast", "Stream"],
    ["roastStream", "Reducer"],
    ["@/lib/", "types"],
    ["fetch", "("],
    ["local", "Storage"],
    ["__last", "EventId"],
    ["Number", "("],
    ["parse", "Int"],
  ].map((parts) => parts.join(""));

  it.each(PRODUCTION_FILES)("%s contains no forbidden literal", (path) => {
    const source = readFileSync(path, "utf8");
    expect(source.length).toBeGreaterThan(0);
    for (const literal of FORBIDDEN_LITERALS) expect(source).not.toContain(literal);
  });
});

describe("F2 route placement", () => {
  it("both cold paths are top-level entries, not children of the operator layout", () => {
    const topLevelPaths = routes.map((route) => route.path);
    expect(topLevelPaths).toContain("/cold-characterisation");
    expect(topLevelPaths).toContain("/__cold-characterisation-harness");
    const nestedPaths = routes.flatMap((route) => (route.children ?? []).map((child) => child.path));
    expect(nestedPaths).not.toContain("/cold-characterisation");
    expect(nestedPaths).not.toContain("/__cold-characterisation-harness");
  });

  it("the product route renders through App as a top-level page with no operator navigation", async () => {
    vi.stubGlobal("EventSource", RecordingSource);
    const { fetchSpy } = renderRoute("/cold-characterisation");
    const view = await screen.findByTestId("cold-view");
    expect(within(view).getByRole("heading", { name: "Cold characterisation" })).toBeInTheDocument();
    expect(screen.queryByRole("navigation")).toBeNull();
    await settle();
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});
