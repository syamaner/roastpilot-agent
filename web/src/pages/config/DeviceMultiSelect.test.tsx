/**
 * DeviceMultiSelect — interaction tests (#419, slice 3a-2).
 *
 * Asserts real behaviour through the open popover (lessons from 3a-1 review):
 *  1. Loaded: option rows visible; toggling fires onChange with the updated array.
 *  2. Add (unchecked → checked): value appears in the new array.
 *  3. Remove (checked → unchecked): value absent from the new array.
 *  4. Popover stays OPEN after a toggle (multi-select behaviour).
 *  5. Checkbox: role="checkbox" + aria-checked reflect checked state.
 *  6. Enter key toggles a row; Space key toggles a row.
 *  7. Trigger toggle while open: clicking trigger again closes the popover.
 *  8. Escape closes the popover.
 *  9. Trigger aria-haspopup="listbox", aria-expanded toggles.
 * 10. Loading body: device-list-loading visible when isPending.
 * 11. Loading trigger: "Scanning for devices…" when isPending or isRefetching.
 * 12. Empty state: "No devices found" + Rescan button.
 * 13. Per-source error: audio_input_error rendered in device-list-error.
 * 14. Query error (GET /api/config/devices fails): device-list-query-error,
 *     NOT device-list-empty.
 * 15. Rescan calls refetch(); button shows "Rescanning…" while isRefetching.
 * 16. Ghost rows: configured values absent from enumerated list appear checked
 *     with a warning note.
 * 17. Chip strip shows selected values below the trigger.
 * 18. Disabled: trigger cannot open the popover.
 * 19. Never free-text: no textbox rendered with popover open.
 */

import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { DevicesSnapshot } from "@/lib/types";
import { DeviceMultiSelect } from "./DeviceMultiSelect";

// ---------------------------------------------------------------------------
// Mock useDevices
// ---------------------------------------------------------------------------

interface DevicesMockReturn {
  data: DevicesSnapshot | undefined;
  isPending: boolean;
  isRefetching: boolean;
  isError: boolean;
  error: Error | null;
  refetch: () => Promise<unknown>;
}
const useDevicesMock = vi.hoisted(() => vi.fn<() => DevicesMockReturn>());

vi.mock("@/hooks/queries", () => ({
  useDevices: useDevicesMock,
  useConfig: vi.fn(),
  useSaveConfig: vi.fn(),
}));

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const AUDIO_DEVICES = [
  { value: "2", label: "USB PnP Sound Device", note: "Input · 1 ch · 48 kHz" },
  { value: "5", label: "ATR2100x-USB",          note: "Input · 2 ch · 44.1 kHz" },
  { value: "8", label: "Built-in Microphone",   note: "Input · 2 ch · 44.1 kHz" },
];

const LOADED_SNAPSHOT: DevicesSnapshot = {
  serial: [],
  serial_error: null,
  audio_input: AUDIO_DEVICES,
  audio_input_error: null,
};

const EMPTY_SNAPSHOT: DevicesSnapshot = {
  serial: [],
  serial_error: null,
  audio_input: [],
  audio_input_error: null,
};

const ERROR_SNAPSHOT: DevicesSnapshot = {
  serial: [],
  serial_error: null,
  audio_input: [],
  audio_input_error: "PortAudio not installed",
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeRefetch(): () => Promise<unknown> {
  return vi.fn<() => Promise<unknown>>().mockResolvedValue(undefined);
}

function defaultMockState(overrides?: Partial<DevicesMockReturn>): void {
  useDevicesMock.mockReturnValue({
    data: LOADED_SNAPSHOT,
    isPending: false,
    isRefetching: false,
    isError: false,
    error: null,
    refetch: makeRefetch(),
    ...overrides,
  });
}

function renderMultiSelect(props?: Partial<React.ComponentProps<typeof DeviceMultiSelect>>) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const onChange = vi.fn<(v: string[]) => void>();
  render(
    <QueryClientProvider client={client}>
      <DeviceMultiSelect
        label="Recording devices"
        values={[]}
        onChange={onChange}
        {...props}
      />
    </QueryClientProvider>,
  );
  return { onChange };
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("DeviceMultiSelect — loaded state", () => {
  beforeEach(() => defaultMockState());

  it("renders the trigger button", () => {
    renderMultiSelect();
    expect(screen.getByTestId("device-multi-select-trigger")).toBeInTheDocument();
  });

  it("opens the popover and shows option rows when the trigger is clicked", () => {
    renderMultiSelect();
    fireEvent.click(screen.getByTestId("device-multi-select-trigger"));
    expect(screen.getByTestId(`device-multi-option-${AUDIO_DEVICES[0]!.value}`)).toBeInTheDocument();
    expect(screen.getByTestId(`device-multi-option-${AUDIO_DEVICES[1]!.value}`)).toBeInTheDocument();
  });

  it("ADD: fires onChange with the value added to the array when an unchecked row is clicked", () => {
    const { onChange } = renderMultiSelect({ values: [] });
    fireEvent.click(screen.getByTestId("device-multi-select-trigger"));
    fireEvent.click(screen.getByTestId(`device-multi-option-${AUDIO_DEVICES[0]!.value}`));
    expect(onChange).toHaveBeenCalledWith([AUDIO_DEVICES[0]!.value]);
  });

  it("REMOVE: fires onChange with the value removed from the array when a checked row is clicked", () => {
    const { onChange } = renderMultiSelect({ values: [AUDIO_DEVICES[0]!.value, AUDIO_DEVICES[1]!.value] });
    fireEvent.click(screen.getByTestId("device-multi-select-trigger"));
    fireEvent.click(screen.getByTestId(`device-multi-option-${AUDIO_DEVICES[0]!.value}`));
    expect(onChange).toHaveBeenCalledWith([AUDIO_DEVICES[1]!.value]);
  });

  it("popover stays open after a toggle (multi-select does not auto-close)", () => {
    renderMultiSelect();
    fireEvent.click(screen.getByTestId("device-multi-select-trigger"));
    fireEvent.click(screen.getByTestId(`device-multi-option-${AUDIO_DEVICES[0]!.value}`));
    expect(screen.getByTestId(`device-multi-option-${AUDIO_DEVICES[0]!.value}`)).toBeInTheDocument();
  });

  it("checked row has aria-selected=true; unchecked row has aria-selected=false; listbox has aria-multiselectable", () => {
    renderMultiSelect({ values: [AUDIO_DEVICES[0]!.value] });
    fireEvent.click(screen.getByTestId("device-multi-select-trigger"));
    const checkedRow = screen.getByTestId(`device-multi-option-${AUDIO_DEVICES[0]!.value}`);
    const uncheckedRow = screen.getByTestId(`device-multi-option-${AUDIO_DEVICES[1]!.value}`);
    // Semantics on the option rows (not the decorative inner box).
    expect(checkedRow).toHaveAttribute("aria-selected", "true");
    expect(uncheckedRow).toHaveAttribute("aria-selected", "false");
    // Listbox declares multi-selectable so the aria-selected pattern is well-formed.
    expect(screen.getByRole("listbox")).toHaveAttribute("aria-multiselectable", "true");
  });

  it("toggles a row via keyboard Enter", () => {
    const { onChange } = renderMultiSelect({ values: [] });
    fireEvent.click(screen.getByTestId("device-multi-select-trigger"));
    const row = screen.getByTestId(`device-multi-option-${AUDIO_DEVICES[0]!.value}`);
    fireEvent.keyDown(row, { key: "Enter" });
    expect(onChange).toHaveBeenCalledWith([AUDIO_DEVICES[0]!.value]);
  });

  it("toggles a row via keyboard Space", () => {
    const { onChange } = renderMultiSelect({ values: [] });
    fireEvent.click(screen.getByTestId("device-multi-select-trigger"));
    const row = screen.getByTestId(`device-multi-option-${AUDIO_DEVICES[0]!.value}`);
    fireEvent.keyDown(row, { key: " " });
    expect(onChange).toHaveBeenCalledWith([AUDIO_DEVICES[0]!.value]);
  });

  it("clicking the trigger while open closes the popover (toggle)", () => {
    renderMultiSelect();
    fireEvent.click(screen.getByTestId("device-multi-select-trigger"));
    expect(screen.getByTestId(`device-multi-option-${AUDIO_DEVICES[0]!.value}`)).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("device-multi-select-trigger"));
    expect(screen.queryByTestId(`device-multi-option-${AUDIO_DEVICES[0]!.value}`)).toBeNull();
  });

  it("closes the popover on Escape", () => {
    renderMultiSelect();
    fireEvent.click(screen.getByTestId("device-multi-select-trigger"));
    expect(screen.getByTestId(`device-multi-option-${AUDIO_DEVICES[0]!.value}`)).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByTestId(`device-multi-option-${AUDIO_DEVICES[0]!.value}`)).toBeNull();
  });

  it("trigger shows count summary when multiple devices are selected", () => {
    renderMultiSelect({ values: [AUDIO_DEVICES[0]!.value, AUDIO_DEVICES[1]!.value] });
    expect(screen.getByTestId("device-multi-select-trigger")).toHaveTextContent("2 devices selected");
  });

  it("trigger shows device label when exactly one device is selected", () => {
    renderMultiSelect({ values: [AUDIO_DEVICES[0]!.value] });
    expect(screen.getByTestId("device-multi-select-trigger")).toHaveTextContent(AUDIO_DEVICES[0]!.label);
  });

  it("chip strip renders selected values below the trigger", () => {
    renderMultiSelect({ values: [AUDIO_DEVICES[0]!.value, AUDIO_DEVICES[1]!.value] });
    const chips = screen.getByTestId("device-multi-select-chips");
    expect(chips).toHaveTextContent(AUDIO_DEVICES[0]!.value);
    expect(chips).toHaveTextContent(AUDIO_DEVICES[1]!.value);
  });
});

describe("DeviceMultiSelect — trigger ARIA + no free-text invariant", () => {
  beforeEach(() => defaultMockState());

  it("trigger has aria-haspopup='listbox' and aria-expanded=false when closed", () => {
    renderMultiSelect();
    const trigger = screen.getByTestId("device-multi-select-trigger");
    expect(trigger).toHaveAttribute("aria-haspopup", "listbox");
    expect(trigger).toHaveAttribute("aria-expanded", "false");
  });

  it("trigger aria-expanded flips to true when the popover opens", () => {
    renderMultiSelect();
    fireEvent.click(screen.getByTestId("device-multi-select-trigger"));
    expect(screen.getByTestId("device-multi-select-trigger")).toHaveAttribute("aria-expanded", "true");
  });

  it("no text input is ever rendered — device selection is enumerated, never free-text", () => {
    renderMultiSelect();
    fireEvent.click(screen.getByTestId("device-multi-select-trigger"));
    expect(screen.queryByRole("textbox")).toBeNull();
  });
});

describe("DeviceMultiSelect — loading state", () => {
  it("shows spinner and scanning message in the trigger when isPending", () => {
    defaultMockState({ data: undefined, isPending: true });
    renderMultiSelect();
    expect(screen.getByTestId("device-multi-select-trigger")).toHaveTextContent("Scanning for devices…");
  });

  it("shows the loading body inside the popover when isPending", () => {
    defaultMockState({ data: undefined, isPending: true });
    renderMultiSelect();
    fireEvent.click(screen.getByTestId("device-multi-select-trigger"));
    expect(screen.getByTestId("device-list-loading")).toBeInTheDocument();
  });

  it("shows spinner and scanning message in the trigger when isRefetching", () => {
    defaultMockState({ isRefetching: true });
    renderMultiSelect();
    expect(screen.getByTestId("device-multi-select-trigger")).toHaveTextContent("Scanning for devices…");
  });
});

describe("DeviceMultiSelect — empty state", () => {
  it("shows 'No devices found' message + Rescan button", () => {
    defaultMockState({ data: EMPTY_SNAPSHOT });
    renderMultiSelect();
    fireEvent.click(screen.getByTestId("device-multi-select-trigger"));
    const empty = screen.getByTestId("device-list-empty");
    expect(empty).toHaveTextContent("No devices found");
    expect(screen.getByTestId("device-rescan-btn")).toBeInTheDocument();
  });
});

describe("DeviceMultiSelect — per-source error state", () => {
  it("renders the audio_input_error string for operator diagnostics, not the empty state", () => {
    defaultMockState({ data: ERROR_SNAPSHOT });
    renderMultiSelect();
    fireEvent.click(screen.getByTestId("device-multi-select-trigger"));
    expect(screen.getByTestId("device-list-error"))
      .toHaveTextContent(ERROR_SNAPSHOT.audio_input_error!);
    expect(screen.queryByTestId("device-list-empty")).toBeNull();
  });
});

describe("DeviceMultiSelect — query error state", () => {
  it("shows a distinct query-error state (not empty) when the request itself fails", () => {
    defaultMockState({
      data: undefined,
      isError: true,
      error: new Error("500 Internal Server Error"),
    });
    renderMultiSelect();
    fireEvent.click(screen.getByTestId("device-multi-select-trigger"));
    expect(screen.getByTestId("device-list-query-error")).toBeInTheDocument();
    expect(screen.getByTestId("device-list-query-error")).toHaveTextContent("Couldn't load devices");
    expect(screen.queryByTestId("device-list-empty")).toBeNull();
  });
});

describe("DeviceMultiSelect — rescan", () => {
  it("calls refetch() when the rescan footer button is clicked", async () => {
    const refetch = makeRefetch();
    defaultMockState({ data: EMPTY_SNAPSHOT, refetch });
    renderMultiSelect();
    fireEvent.click(screen.getByTestId("device-multi-select-trigger"));
    fireEvent.click(screen.getByTestId("device-rescan-btn"));
    await waitFor(() => expect(refetch).toHaveBeenCalledTimes(1));
  });

  it("shows 'Rescanning…' in the footer while isRefetching", () => {
    defaultMockState({ data: EMPTY_SNAPSHOT, isRefetching: true });
    renderMultiSelect();
    fireEvent.click(screen.getByTestId("device-multi-select-trigger"));
    expect(screen.getByTestId("device-rescan-btn")).toHaveTextContent("Rescanning…");
  });
});

describe("DeviceMultiSelect — ghost rows", () => {
  it("shows a configured value absent from the enumerated list as checked with a warning", () => {
    defaultMockState();
    const ghostValue = "99";
    renderMultiSelect({ values: [ghostValue] });
    fireEvent.click(screen.getByTestId("device-multi-select-trigger"));
    const row = screen.getByTestId(`device-multi-option-${ghostValue}`);
    expect(row).toBeInTheDocument();
    expect(row).toHaveAttribute("aria-selected", "true");
    expect(row).toHaveTextContent("Not found on rescan — previously configured");
  });
});

describe("DeviceMultiSelect — disabled", () => {
  it("does not open the popover when disabled", () => {
    defaultMockState();
    renderMultiSelect({ disabled: true });
    fireEvent.click(screen.getByTestId("device-multi-select-trigger"));
    expect(screen.queryByTestId(`device-multi-option-${AUDIO_DEVICES[0]!.value}`)).toBeNull();
  });
});
