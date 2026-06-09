import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AddBeansToast } from "./AddBeansToast";

afterEach(cleanup);

const GUIDANCE = { bean_temp_c: 185, env_temp_c: 195, guidance_min_c: 170, guidance_max_c: 200 };

describe("AddBeansToast", () => {
  it("renders nothing when there is no guidance", () => {
    render(<AddBeansToast guidance={null} visible onDismiss={() => {}} />);
    expect(screen.queryByTestId("add-beans-toast")).toBeNull();
  });

  it("renders nothing when not visible (dismissed)", () => {
    render(<AddBeansToast guidance={GUIDANCE} visible={false} onDismiss={() => {}} />);
    expect(screen.queryByTestId("add-beans-toast")).toBeNull();
  });

  it("shows the charge-zone guidance with the band range", () => {
    render(<AddBeansToast guidance={GUIDANCE} visible onDismiss={() => {}} />);
    const toast = screen.getByTestId("add-beans-toast");
    expect(toast).toHaveTextContent(/charge zone reached/i);
    expect(toast).toHaveTextContent("170.0 °C");
    expect(toast).toHaveTextContent("200.0 °C");
  });

  it("dismisses on click (non-blocking guidance)", () => {
    const onDismiss = vi.fn();
    render(<AddBeansToast guidance={GUIDANCE} visible onDismiss={onDismiss} />);
    fireEvent.click(screen.getByTestId("add-beans-dismiss"));
    expect(onDismiss).toHaveBeenCalledOnce();
  });
});
