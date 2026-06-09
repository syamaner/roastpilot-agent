import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import { FIXTURE_DETAIL, FIXTURE_TELEMETRY, FIXTURE_TIMELINE } from "./fixture";
import { DetailPage } from "./DetailPage";

function renderAt(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const Wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/roasts/:runId" element={children} />
          <Route path="/roasts" element={children} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
  return render(<DetailPage />, { wrapper: Wrapper });
}

afterEach(() => vi.restoreAllMocks());

describe("DetailPage shell", () => {
  it("fetches by the route run id and renders the detail view", async () => {
    const detailSpy = vi.spyOn(api, "roast").mockResolvedValue(FIXTURE_DETAIL);
    vi.spyOn(api, "telemetry").mockResolvedValue(FIXTURE_TELEMETRY);
    vi.spyOn(api, "timeline").mockResolvedValue(FIXTURE_TIMELINE);

    renderAt(`/roasts/${FIXTURE_DETAIL.id}`);

    await waitFor(() => expect(screen.getByTestId("detail-view")).toBeInTheDocument());
    expect(detailSpy).toHaveBeenCalledWith(FIXTURE_DETAIL.id);
    expect(screen.getByTestId("decision-trace-table")).toBeInTheDocument();
  });

  it("shows a not-found message when the detail query errors", async () => {
    vi.spyOn(api, "roast").mockRejectedValue(new Error("404"));
    vi.spyOn(api, "telemetry").mockResolvedValue(FIXTURE_TELEMETRY);
    vi.spyOn(api, "timeline").mockResolvedValue(FIXTURE_TIMELINE);

    renderAt(`/roasts/${FIXTURE_DETAIL.id}`);
    await waitFor(() => expect(screen.getByTestId("detail-error")).toBeInTheDocument());
  });

  it("shows a no-run message when there is no run id", () => {
    renderAt("/roasts");
    expect(screen.getByTestId("detail-no-run")).toBeInTheDocument();
  });
});
