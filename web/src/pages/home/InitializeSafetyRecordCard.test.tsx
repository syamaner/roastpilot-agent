import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { InitializeSafetyRecordCard } from "./InitializeSafetyRecordCard";

afterEach(cleanup);

const CONFIRMED_RESULT = {
  outcome: "confirmed" as const,
  restart_clearance: {
    state: "required" as const,
    eligible: false,
    enabled_actions: [],
    closure_only_available: false,
  },
  fault_controls: { status: "closed" as const, generation: 1, enabled_actions: [] },
};

describe("InitializeSafetyRecordCard", () => {
  it("renders only from the server action projection", () => {
    render(<InitializeSafetyRecordCard available={false} onInitialize={async () => CONFIRMED_RESULT} />);
    expect(screen.queryByTestId("initialize-safety-record")).toBeNull();
  });

  it("requires fresh three-part confirmation and reports the separate D213 step", async () => {
    const onInitialize = vi.fn(async () => CONFIRMED_RESULT);
    const onRefreshStatus = vi.fn();
    render(
      <InitializeSafetyRecordCard
        available
        onInitialize={onInitialize}
        onRefreshStatus={onRefreshStatus}
      />,
    );

    expect(screen.getByText(/Confirm stopped roaster and initialise safety record/i)).toBeVisible();
    fireEvent.click(screen.getByTestId("initialize-safety-record-open"));
    const submit = screen.getByTestId("initialize-safety-record-submit");
    expect(submit).toBeDisabled();
    fireEvent.click(screen.getByTestId("initialize-safety-record-stopped"));
    fireEvent.click(screen.getByTestId("initialize-safety-record-empty"));
    fireEvent.click(screen.getByTestId("initialize-safety-record-independent-stop"));
    expect(submit).toBeEnabled();
    fireEvent.click(submit);

    await waitFor(() => expect(onInitialize).toHaveBeenCalledTimes(1));
    expect(screen.getByTestId("initialize-safety-record-reported")).toHaveTextContent(
      /new ordinary restart clearance/i,
    );
    fireEvent.click(screen.getByTestId("initialize-safety-record-refresh-status"));
    expect(onRefreshStatus).toHaveBeenCalledTimes(1);
  });

  it("resets confirmation when the server projection becomes stale", () => {
    const { rerender } = render(
      <InitializeSafetyRecordCard available onInitialize={async () => CONFIRMED_RESULT} />,
    );
    fireEvent.click(screen.getByTestId("initialize-safety-record-open"));
    fireEvent.click(screen.getByTestId("initialize-safety-record-stopped"));
    expect(screen.getByTestId("initialize-safety-record-stopped")).toBeChecked();

    rerender(<InitializeSafetyRecordCard available={false} onInitialize={async () => CONFIRMED_RESULT} />);
    expect(screen.queryByTestId("initialize-safety-record")).toBeNull();
    rerender(<InitializeSafetyRecordCard available onInitialize={async () => CONFIRMED_RESULT} />);
    fireEvent.click(screen.getByTestId("initialize-safety-record-open"));
    expect(screen.getByTestId("initialize-safety-record-stopped")).not.toBeChecked();
  });

  it("submits one physical confirmation only once while the request is in flight", async () => {
    let resolveRequest: (() => void) | undefined;
    const onInitialize = vi.fn(
      () =>
        new Promise<typeof CONFIRMED_RESULT>((resolve) => {
          resolveRequest = () => resolve(CONFIRMED_RESULT);
        }),
    );
    render(<InitializeSafetyRecordCard available onInitialize={onInitialize} />);
    fireEvent.click(screen.getByTestId("initialize-safety-record-open"));
    fireEvent.click(screen.getByTestId("initialize-safety-record-stopped"));
    fireEvent.click(screen.getByTestId("initialize-safety-record-empty"));
    fireEvent.click(screen.getByTestId("initialize-safety-record-independent-stop"));
    const submit = screen.getByTestId("initialize-safety-record-submit");
    fireEvent.click(submit);
    fireEvent.click(submit);
    expect(onInitialize).toHaveBeenCalledTimes(1);
    resolveRequest?.();
    await waitFor(() => expect(screen.getByTestId("initialize-safety-record-reported")).toBeVisible());
  });
});
