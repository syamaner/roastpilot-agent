import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { FaultAcknowledgementControl } from "./FaultAcknowledgementControl";

afterEach(cleanup);

describe("FaultAcknowledgementControl", () => {
  it("requires an explicit cooling-safe confirmation before dispatching", () => {
    const onConfirm = vi.fn();
    render(<FaultAcknowledgementControl status={{ kind: "ready" }} onConfirm={onConfirm} />);

    const button = screen.getByTestId("fault-acknowledge");
    expect(button).toHaveTextContent("Stop cooling and acknowledge");
    fireEvent.click(button);
    expect(onConfirm).not.toHaveBeenCalled();
    expect(screen.getByTestId("fault-acknowledgement-confirmation")).toHaveTextContent(
      "safe to end cooling",
    );
    expect(button).toHaveTextContent("Confirm cooling is safe to end");

    fireEvent.click(button);
    expect(onConfirm).toHaveBeenCalledOnce();
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(onConfirm).toHaveBeenCalledOnce();
  });

  it("does not treat a queued action as completed", () => {
    render(<FaultAcknowledgementControl status={{ kind: "pending" }} onConfirm={() => {}} />);

    expect(screen.queryByTestId("fault-acknowledge")).toBeNull();
    expect(screen.getByTestId("fault-acknowledgement-pending")).toHaveTextContent(
      "Waiting for the server",
    );
  });

  it("renders only server-provided failure or completion outcomes", () => {
    const { rerender } = render(
      <FaultAcknowledgementControl
        status={{ kind: "failed", reason: "unsafe_state" }}
        onConfirm={() => {}}
      />,
    );
    expect(screen.getByTestId("fault-acknowledgement-failed")).toHaveTextContent(
      "unsafe_state",
    );

    rerender(<FaultAcknowledgementControl status={{ kind: "completed" }} onConfirm={() => {}} />);
    expect(screen.getByTestId("fault-acknowledgement-completed")).toHaveTextContent(
      "Verified cooling is off and fault acknowledged",
    );
  });
});
