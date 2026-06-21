import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { BeanProfilePicker } from "./BeanProfilePicker";
import { FIXTURE_BEAN_PROFILES } from "./beanProfileFixture";

afterEach(cleanup);

describe("BeanProfilePicker (#303)", () => {
  it("renders the saved library as options incl. the Ethiopia Koke seed", () => {
    render(
      <BeanProfilePicker
        profiles={FIXTURE_BEAN_PROFILES}
        selectedId=""
        onSelect={vi.fn()}
        onAdd={vi.fn()}
        onEdit={vi.fn()}
      />,
    );
    const select = screen.getByTestId("bean-profile-select");
    expect(select).toHaveTextContent("Ethiopia Yirgacheffe Koke (Natural)");
    expect(select).toHaveTextContent("Colombia Huila (Washed)");
  });

  it("fires onSelect with the chosen profile id", () => {
    const onSelect = vi.fn();
    render(
      <BeanProfilePicker
        profiles={FIXTURE_BEAN_PROFILES}
        selectedId=""
        onSelect={onSelect}
        onAdd={vi.fn()}
        onEdit={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByTestId("bean-profile-select"), {
      target: { value: "seed-ethiopia-yirgacheffe-koke-natural" },
    });
    expect(onSelect).toHaveBeenCalledWith("seed-ethiopia-yirgacheffe-koke-natural");
  });

  it("opens the add modal via the Add button", () => {
    const onAdd = vi.fn();
    render(
      <BeanProfilePicker
        profiles={FIXTURE_BEAN_PROFILES}
        selectedId=""
        onSelect={vi.fn()}
        onAdd={onAdd}
        onEdit={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByTestId("bean-profile-add-button"));
    expect(onAdd).toHaveBeenCalledTimes(1);
  });

  it("disables Edit until a profile is selected, then enables + fires onEdit", () => {
    const onEdit = vi.fn();
    const { rerender } = render(
      <BeanProfilePicker
        profiles={FIXTURE_BEAN_PROFILES}
        selectedId=""
        onSelect={vi.fn()}
        onAdd={vi.fn()}
        onEdit={onEdit}
      />,
    );
    expect(screen.getByTestId("bean-profile-edit-button")).toBeDisabled();
    rerender(
      <BeanProfilePicker
        profiles={FIXTURE_BEAN_PROFILES}
        selectedId="profile-colombia-huila-washed"
        onSelect={vi.fn()}
        onAdd={vi.fn()}
        onEdit={onEdit}
      />,
    );
    const edit = screen.getByTestId("bean-profile-edit-button");
    expect(edit).toBeEnabled();
    fireEvent.click(edit);
    expect(onEdit).toHaveBeenCalledTimes(1);
  });

  it("disables the controls while the library is loading", () => {
    render(
      <BeanProfilePicker
        profiles={[]}
        selectedId=""
        onSelect={vi.fn()}
        onAdd={vi.fn()}
        onEdit={vi.fn()}
        loading
      />,
    );
    expect(screen.getByTestId("bean-profile-select")).toBeDisabled();
    expect(screen.getByTestId("bean-profile-add-button")).toBeDisabled();
  });
});
