import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api";
import type { BeanProfile, BeanProfileInput } from "@/lib/types";
import { BeanProfileModal } from "./BeanProfileModal";
import { FIXTURE_KOKE } from "./beanProfileFixture";

afterEach(cleanup);

/** A saved BeanProfile echoed back by the mocked save (the server response). */
function savedFrom(input: BeanProfileInput): BeanProfile {
  return { ...input, id: "new-id", created_at: "t", updated_at: "t" };
}

describe("BeanProfileModal add mode (#303)", () => {
  it("POSTs the captured input and reports the saved profile", async () => {
    const onSave = vi.fn(async (input: BeanProfileInput) => savedFrom(input));
    const onSaved = vi.fn();
    render(
      <BeanProfileModal mode="add" onSave={onSave} onSaved={onSaved} onClose={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("bean-profile-name"), {
      target: { value: "House blend" },
    });
    fireEvent.change(screen.getByTestId("bean-profile-bean_origin"), {
      target: { value: "Brazil" },
    });
    fireEvent.submit(screen.getByTestId("bean-profile-form"));

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    const input = onSave.mock.calls[0][0];
    expect(input.name).toBe("House blend");
    expect(input.bean_origin).toBe("Brazil");
    // The modal owns the default charge weight (pre-filled 250).
    expect(input.default_bean_weight_grams).toBe(250);
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
  });

  it("captures the product URL on the saved profile (#315)", async () => {
    const onSave = vi.fn(async (input: BeanProfileInput) => savedFrom(input));
    render(
      <BeanProfileModal mode="add" onSave={onSave} onSaved={vi.fn()} onClose={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("bean-profile-name"), { target: { value: "Kenya" } });
    fireEvent.change(screen.getByTestId("bean-profile-bean_origin"), {
      target: { value: "Kenya" },
    });
    fireEvent.change(screen.getByTestId("bean-profile-source_url"), {
      target: { value: "https://roaster.example.com/kenya-aa" },
    });
    fireEvent.submit(screen.getByTestId("bean-profile-form"));
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave.mock.calls[0][0].source_url).toBe("https://roaster.example.com/kenya-aa");
  });

  it("blocks save and shows field errors when required fields are blank", () => {
    const onSave = vi.fn();
    render(
      <BeanProfileModal mode="add" onSave={onSave} onSaved={vi.fn()} onClose={vi.fn()} />,
    );
    fireEvent.submit(screen.getByTestId("bean-profile-form"));
    expect(screen.getByTestId("bean-profile-name-error")).toBeInTheDocument();
    expect(screen.getByTestId("bean-profile-bean_origin-error")).toBeInTheDocument();
    expect(onSave).not.toHaveBeenCalled();
  });

  it("surfaces a 422 from the server inline", async () => {
    const onSave = vi.fn().mockRejectedValue(new ApiError(422, "invalid charge band"));
    render(
      <BeanProfileModal mode="add" onSave={onSave} onSaved={vi.fn()} onClose={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("bean-profile-name"), { target: { value: "X" } });
    fireEvent.change(screen.getByTestId("bean-profile-bean_origin"), {
      target: { value: "Y" },
    });
    fireEvent.submit(screen.getByTestId("bean-profile-form"));
    await waitFor(() =>
      expect(screen.getByTestId("bean-profile-error")).toHaveTextContent(/invalid charge band/i),
    );
  });

  it("does not offer Archive in add mode", () => {
    render(
      <BeanProfileModal
        mode="add"
        onSave={vi.fn()}
        onSaved={vi.fn()}
        onClose={vi.fn()}
        onArchive={vi.fn()}
      />,
    );
    expect(screen.queryByTestId("bean-profile-archive")).toBeNull();
  });
});

describe("BeanProfileModal edit mode (#303)", () => {
  it("pre-fills from the profile and PUTs the edited input", async () => {
    const onSave = vi.fn(async (input: BeanProfileInput) => savedFrom(input));
    render(
      <BeanProfileModal
        mode="edit"
        profile={FIXTURE_KOKE}
        onSave={onSave}
        onSaved={vi.fn()}
        onClose={vi.fn()}
      />,
    );
    // Pre-filled from the selected profile.
    expect(screen.getByTestId("bean-profile-name")).toHaveValue(FIXTURE_KOKE.name);
    expect(screen.getByTestId("bean-profile-default_bean_weight_grams")).toHaveValue(250);
    // Edit a field and save.
    fireEvent.change(screen.getByTestId("bean-profile-target_drop_temp_c"), {
      target: { value: "192" },
    });
    fireEvent.submit(screen.getByTestId("bean-profile-form"));
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave.mock.calls[0][0].target_drop_temp_c).toBe(192);
  });

  it("shows the future-roasts-only note in edit mode", () => {
    render(
      <BeanProfileModal
        mode="edit"
        profile={FIXTURE_KOKE}
        onSave={vi.fn()}
        onSaved={vi.fn()}
        onClose={vi.fn()}
      />,
    );
    expect(screen.getByText(/affect future roasts only/i)).toBeInTheDocument();
  });

  it("archives via the Archive button when onArchive is provided", async () => {
    const onArchive = vi.fn().mockResolvedValue({ id: FIXTURE_KOKE.id, result: "archived" });
    const onClose = vi.fn();
    render(
      <BeanProfileModal
        mode="edit"
        profile={FIXTURE_KOKE}
        onSave={vi.fn()}
        onSaved={vi.fn()}
        onClose={onClose}
        onArchive={onArchive}
      />,
    );
    fireEvent.click(screen.getByTestId("bean-profile-archive"));
    await waitFor(() => expect(onArchive).toHaveBeenCalledWith(FIXTURE_KOKE.id));
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  });

  it("closes without saving via Cancel", () => {
    const onClose = vi.fn();
    const onSave = vi.fn();
    render(
      <BeanProfileModal
        mode="edit"
        profile={FIXTURE_KOKE}
        onSave={onSave}
        onSaved={vi.fn()}
        onClose={onClose}
      />,
    );
    fireEvent.click(screen.getByTestId("bean-profile-cancel"));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onSave).not.toHaveBeenCalled();
  });
});
