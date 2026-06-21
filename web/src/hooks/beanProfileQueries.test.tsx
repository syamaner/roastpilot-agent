import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import type { BeanProfile, BeanProfileInput } from "@/lib/types";
import {
  beanProfileKeys,
  useBeanProfiles,
  useCreateBeanProfile,
  useDeleteBeanProfile,
  useUpdateBeanProfile,
} from "./queries";

function makeClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

function wrapperFor(client: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

const INPUT: BeanProfileInput = {
  name: "Test",
  bean_origin: "Ethiopia",
  bean_varietal: null,
  charge_guidance_min_c: 170,
  charge_guidance_max_c: 200,
  initial_heat_percent: 70,
  initial_fan_percent: 40,
  target_drop_temp_c: 195,
  target_development_percent: 15,
  default_bean_weight_grams: 250,
};

const SAVED: BeanProfile = { ...INPUT, id: "p1", created_at: "t", updated_at: "t" };

afterEach(() => vi.restoreAllMocks());

describe("useBeanProfiles", () => {
  it("fetches the saved bean-profile library", async () => {
    const spy = vi
      .spyOn(api, "beanProfiles")
      .mockResolvedValue({ profiles: [SAVED] });
    renderHook(() => useBeanProfiles(), { wrapper: wrapperFor(makeClient()) });
    await waitFor(() => expect(spy).toHaveBeenCalled());
  });
});

describe("useCreateBeanProfile", () => {
  it("POSTs the input and invalidates the list query", async () => {
    const post = vi.spyOn(api, "createBeanProfile").mockResolvedValue(SAVED);
    const client = makeClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const { result } = renderHook(() => useCreateBeanProfile(), {
      wrapper: wrapperFor(client),
    });
    const saved = await result.current.mutateAsync(INPUT);
    expect(post).toHaveBeenCalledWith(INPUT);
    expect(saved.id).toBe("p1");
    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({ queryKey: beanProfileKeys.all }),
    );
  });
});

describe("useUpdateBeanProfile", () => {
  it("PUTs id+input and invalidates the list query", async () => {
    const put = vi.spyOn(api, "updateBeanProfile").mockResolvedValue(SAVED);
    const client = makeClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const { result } = renderHook(() => useUpdateBeanProfile(), {
      wrapper: wrapperFor(client),
    });
    await result.current.mutateAsync({ id: "p1", input: INPUT });
    expect(put).toHaveBeenCalledWith("p1", INPUT);
    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({ queryKey: beanProfileKeys.all }),
    );
  });
});

describe("useDeleteBeanProfile", () => {
  it("DELETEs by id and invalidates the list query", async () => {
    const del = vi
      .spyOn(api, "deleteBeanProfile")
      .mockResolvedValue({ id: "p1", result: "archived" });
    const client = makeClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const { result } = renderHook(() => useDeleteBeanProfile(), {
      wrapper: wrapperFor(client),
    });
    await result.current.mutateAsync("p1");
    expect(del).toHaveBeenCalledWith("p1");
    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({ queryKey: beanProfileKeys.all }),
    );
  });
});
