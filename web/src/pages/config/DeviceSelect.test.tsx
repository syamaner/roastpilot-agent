/**
 * DeviceSelect — interaction tests (#419, slice 3a-1).
 *
 * Asserts real behavior, not just render:
 *  1. Loaded state: option rows visible; selecting fires onChange with the device value.
 *  2. Selection closes the popover and focuses the trigger.
 *  3. Selected row has aria-selected=true; unselected rows have aria-selected=false.
 *  4. Monospace value line rendered below the trigger when a device is configured.
 *  5. Keyboard Enter selects an option row.
 *  6. Escape closes the popover.
 *  7. Loading state: trigger shows spinner + scanning message.
 *  8. Empty state: "No devices found" message + diagnostic hint + Rescan button.
 *  9. Error state: per-source error string rendered for operator diagnostics.
 * 10. Rescan calls refetch(); button shows "Rescanning…" while isRefetching.
 * 11. Unavailable ghost row: a configured value absent from the enumerated list
 *     appears as a selected-but-unavailable row with a warning note.
 * 12. Disabled: trigger cannot open the popover.
 * 13. audio_input deviceKind draws from audio_input list + error.
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
import { DeviceSelect } from "./DeviceSelect";

// ---------------------------------------------------------------------------
// Mock useDevices
// ---------------------------------------------------------------------------

interface DevicesMockReturn {
  data: DevicesSnapshot | undefined;
  isPending: boolean;
  isRefetching: boolean;
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

const SERIAL_DEVICES = [
  { value: "/dev/cu.usbserial-DN016OJ3", label: "/dev/cu.usbserial-DN016OJ3", note: "Hottop · FT232R" },
  { value: "/dev/cu.usbmodem14101",      label: "/dev/cu.usbmodem14101",      note: "Hottop · CH340" },
];

const AUDIO_DEVICES = [
  { value: "2", label: "USB PnP Sound Device",  note: "Input · 1 ch · 48 kHz" },
  { value: "5", label: "ATR2100x-USB",           note: "Input · 2 ch · 44.1 kHz" },
];

const LOADED_SNAPSHOT: DevicesSnapshot = {
  serial: SERIAL_DEVICES,
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
  serial_error: "No module named 'serial.tools.list_ports'",
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
    refetch: makeRefetch(),
    ...overrides,
  });
}

function renderSelect(props?: Partial<React.ComponentProps<typeof DeviceSelect>>) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const onChange = vi.fn<(v: string) => void>();
  render(
    <QueryClientProvider client={client}>
      <DeviceSelect
        label="Serial port"
        value=""
        deviceKind="serial"
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

describe("DeviceSelect — loaded state", () => {
  beforeEach(() => defaultMockState());

  it("renders the trigger button with the label", () => {
    renderSelect();
    expect(screen.getByTestId("device-select-trigger")).toBeInTheDocument();
  });

  it("opens the popover and shows option rows when the trigger is clicked", () => {
    renderSelect();
    fireEvent.click(screen.getByTestId("device-select-trigger"));
    expect(screen.getByTestId(`device-option-${SERIAL_DEVICES[0]!.value}`)).toBeInTheDocument();
    expect(screen.getByTestId(`device-option-${SERIAL_DEVICES[1]!.value}`)).toBeInTheDocument();
  });

  it("fires onChange with the device value when an option row is clicked", () => {
    const { onChange } = renderSelect();
    fireEvent.click(screen.getByTestId("device-select-trigger"));
    fireEvent.click(screen.getByTestId(`device-option-${SERIAL_DEVICES[0]!.value}`));
    expect(onChange).toHaveBeenCalledWith(SERIAL_DEVICES[0]!.value);
  });

  it("closes the popover and returns focus to the trigger after a selection", () => {
    renderSelect();
    fireEvent.click(screen.getByTestId("device-select-trigger"));
    fireEvent.click(screen.getByTestId(`device-option-${SERIAL_DEVICES[0]!.value}`));
    expect(screen.queryByTestId(`device-option-${SERIAL_DEVICES[0]!.value}`)).toBeNull();
    expect(screen.getByTestId("device-select-trigger")).toHaveFocus();
  });

  it("marks the currently-selected row aria-selected=true; others aria-selected=false", () => {
    renderSelect({ value: SERIAL_DEVICES[0]!.value });
    fireEvent.click(screen.getByTestId("device-select-trigger"));
    expect(screen.getByTestId(`device-option-${SERIAL_DEVICES[0]!.value}`))
      .toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId(`device-option-${SERIAL_DEVICES[1]!.value}`))
      .toHaveAttribute("aria-selected", "false");
  });

  it("renders a monospace value line below the trigger when a device is configured", () => {
    renderSelect({ value: SERIAL_DEVICES[0]!.value });
    expect(screen.getByTestId("device-select-value"))
      .toHaveTextContent(SERIAL_DEVICES[0]!.value);
  });

  it("selects an option via keyboard Enter", () => {
    const { onChange } = renderSelect();
    fireEvent.click(screen.getByTestId("device-select-trigger"));
    const row = screen.getByTestId(`device-option-${SERIAL_DEVICES[0]!.value}`);
    fireEvent.keyDown(row, { key: "Enter" });
    expect(onChange).toHaveBeenCalledWith(SERIAL_DEVICES[0]!.value);
  });

  it("selects an option via keyboard Space", () => {
    const { onChange } = renderSelect();
    fireEvent.click(screen.getByTestId("device-select-trigger"));
    const row = screen.getByTestId(`device-option-${SERIAL_DEVICES[0]!.value}`);
    fireEvent.keyDown(row, { key: " " });
    expect(onChange).toHaveBeenCalledWith(SERIAL_DEVICES[0]!.value);
  });

  it("closes the popover on Escape", () => {
    renderSelect();
    fireEvent.click(screen.getByTestId("device-select-trigger"));
    expect(screen.getByTestId(`device-option-${SERIAL_DEVICES[0]!.value}`)).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByTestId(`device-option-${SERIAL_DEVICES[0]!.value}`)).toBeNull();
  });

  it("draws from audio_input list when deviceKind='audio_input'", () => {
    renderSelect({ deviceKind: "audio_input" });
    fireEvent.click(screen.getByTestId("device-select-trigger"));
    expect(screen.getByTestId(`device-option-${AUDIO_DEVICES[0]!.value}`)).toBeInTheDocument();
    expect(screen.queryByTestId(`device-option-${SERIAL_DEVICES[0]!.value}`)).toBeNull();
  });
});

describe("DeviceSelect — trigger ARIA + no free-text invariant", () => {
  beforeEach(() => defaultMockState());

  it("trigger has aria-haspopup='listbox' and aria-expanded=false when closed", () => {
    renderSelect();
    const trigger = screen.getByTestId("device-select-trigger");
    expect(trigger).toHaveAttribute("aria-haspopup", "listbox");
    expect(trigger).toHaveAttribute("aria-expanded", "false");
  });

  it("trigger aria-expanded flips to true when the popover opens", () => {
    renderSelect();
    fireEvent.click(screen.getByTestId("device-select-trigger"));
    expect(screen.getByTestId("device-select-trigger")).toHaveAttribute("aria-expanded", "true");
  });

  it("no text input is ever rendered — device selection is enumerated, never free-text", () => {
    renderSelect();
    fireEvent.click(screen.getByTestId("device-select-trigger"));
    expect(screen.queryByRole("textbox")).toBeNull();
  });
});

describe("DeviceSelect — loading state", () => {
  it("shows spinner and scanning message in the trigger when isPending", () => {
    defaultMockState({ data: undefined, isPending: true });
    renderSelect();
    expect(screen.getByTestId("device-select-trigger")).toHaveTextContent("Scanning for devices…");
  });

  it("shows the loading body (spinner div) inside the popover when isPending", () => {
    defaultMockState({ data: undefined, isPending: true });
    renderSelect();
    fireEvent.click(screen.getByTestId("device-select-trigger"));
    expect(screen.getByTestId("device-list-loading")).toBeInTheDocument();
  });

  it("shows spinner and scanning message in the trigger when isRefetching", () => {
    defaultMockState({ isRefetching: true });
    renderSelect();
    expect(screen.getByTestId("device-select-trigger")).toHaveTextContent("Scanning for devices…");
  });
});

describe("DeviceSelect — empty state", () => {
  it("shows 'No devices found' message + diagnostic hint + Rescan button", () => {
    defaultMockState({ data: EMPTY_SNAPSHOT });
    renderSelect();
    fireEvent.click(screen.getByTestId("device-select-trigger"));
    const empty = screen.getByTestId("device-list-empty");
    expect(empty).toHaveTextContent("No devices found");
    expect(empty).toHaveTextContent("Check the USB connection to the roaster, then rescan.");
    expect(screen.getByTestId("device-rescan-btn")).toBeInTheDocument();
  });
});

describe("DeviceSelect — error state", () => {
  it("renders the serial_error string for operator diagnostics", () => {
    defaultMockState({ data: ERROR_SNAPSHOT });
    renderSelect({ deviceKind: "serial" });
    fireEvent.click(screen.getByTestId("device-select-trigger"));
    expect(screen.getByTestId("device-list-error"))
      .toHaveTextContent(ERROR_SNAPSHOT.serial_error!);
  });

  it("renders the audio_input_error string for deviceKind='audio_input'", () => {
    defaultMockState({ data: ERROR_SNAPSHOT });
    renderSelect({ deviceKind: "audio_input" });
    fireEvent.click(screen.getByTestId("device-select-trigger"));
    expect(screen.getByTestId("device-list-error"))
      .toHaveTextContent(ERROR_SNAPSHOT.audio_input_error!);
  });
});

describe("DeviceSelect — rescan", () => {
  it("calls refetch() when the rescan footer button is clicked", async () => {
    const refetch = makeRefetch();
    defaultMockState({ refetch });
    renderSelect();
    fireEvent.click(screen.getByTestId("device-select-trigger"));
    fireEvent.click(screen.getByTestId("device-rescan-btn"));
    await waitFor(() => expect(refetch).toHaveBeenCalledTimes(1));
  });

  it("shows 'Rescanning…' in the footer while isRefetching", () => {
    defaultMockState({ data: EMPTY_SNAPSHOT, isRefetching: true });
    renderSelect();
    fireEvent.click(screen.getByTestId("device-select-trigger"));
    expect(screen.getByTestId("device-rescan-btn")).toHaveTextContent("Rescanning…");
  });
});

describe("DeviceSelect — unavailable ghost value", () => {
  it("shows the configured value as a ghost row with a warning when not in the enumerated list", () => {
    defaultMockState();
    const ghostValue = "/dev/cu.usbserial-GHOST";
    renderSelect({ value: ghostValue });
    fireEvent.click(screen.getByTestId("device-select-trigger"));
    const row = screen.getByTestId(`device-option-${ghostValue}`);
    expect(row).toBeInTheDocument();
    expect(row).toHaveAttribute("aria-selected", "true");
    expect(row).toHaveTextContent("Not found on rescan — previously configured");
  });
});

describe("DeviceSelect — disabled", () => {
  it("does not open the popover when the disabled prop is set", () => {
    defaultMockState();
    renderSelect({ disabled: true });
    fireEvent.click(screen.getByTestId("device-select-trigger"));
    expect(screen.queryByTestId(`device-option-${SERIAL_DEVICES[0]!.value}`)).toBeNull();
  });
});
