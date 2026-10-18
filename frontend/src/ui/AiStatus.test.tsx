import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { AiRuntimeProvider, AiStatus } from "./AiStatus";

const mock = vi.hoisted(() => ({ aiRuntime: vi.fn() }));
vi.mock("../api", () => ({
  api: mock,
  ApiError: class ApiError extends Error {
    constructor(readonly status: number) {
      super();
    }
  },
}));
beforeEach(() => mock.aiRuntime.mockReset());
const live = {
  mode: "live",
  provider: "openai-compatible",
  models: { extraction: "gpt-5.4-mini", synthesis: "gpt-5.4" },
  completion_status: "unchecked",
  embedding_mode: "live",
};

describe("AI runtime honesty", () => {
  test("does not describe a ready stub as working AI", async () => {
    mock.aiRuntime.mockResolvedValue({
      ...live,
      mode: "demo",
      completion_status: "ready",
    });
    render(
      <AiRuntimeProvider>
        <AiStatus />
      </AiRuntimeProvider>,
    );
    expect(
      await screen.findByText("Preview mode · AI not connected"),
    ).toBeTruthy();
    expect(screen.getByText(/cannot provide real AI answers/)).toBeTruthy();
    expect(screen.queryByText("AI connected")).toBeNull();
  });
  test("separates configured models from a verified successful connection", async () => {
    mock.aiRuntime.mockResolvedValue(live);
    render(
      <AiRuntimeProvider>
        <AiStatus />
        <AiStatus />
      </AiRuntimeProvider>,
    );
    await screen.findAllByText("AI configured");
    expect(screen.queryByText("AI connected")).toBeNull();
    expect(mock.aiRuntime).toHaveBeenCalledTimes(1);
  });
  test("connection status failure has a usable retry", async () => {
    mock.aiRuntime
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce({ ...live, completion_status: "ready" });
    render(
      <AiRuntimeProvider>
        <AiStatus />
      </AiRuntimeProvider>,
    );
    await screen.findByText("AI status unavailable");
    fireEvent.click(screen.getByRole("button", { name: "Check connection" }));
    expect(await screen.findByText("AI connected")).toBeTruthy();
  });
  test("hosted mode does not query or claim the local provider state", async () => {
    render(
      <AiRuntimeProvider enabled={false}>
        <AiStatus />
      </AiRuntimeProvider>,
    );
    await waitFor(() => expect(mock.aiRuntime).not.toHaveBeenCalled());
    expect(screen.queryByRole("status")).toBeNull();
  });

  test("compact live status keeps connection details behind a disclosure", async () => {
    mock.aiRuntime.mockResolvedValue({ ...live, completion_status: "ready" });
    render(<AiRuntimeProvider><AiStatus compact /></AiRuntimeProvider>);
    const summary = await screen.findByText("AI connected");
    expect(summary.closest("details")?.open).toBe(false);
    fireEvent.click(summary);
    expect(summary.closest("details")?.open).toBe(true);
    expect(screen.getByRole("button", { name: "Check connection" })).toBeTruthy();
  });

  test("compact mode keeps demo warnings visible", async () => {
    mock.aiRuntime.mockResolvedValue({ ...live, mode: "demo", completion_status: "ready" });
    render(<AiRuntimeProvider><AiStatus compact /></AiRuntimeProvider>);
    expect(await screen.findByText(/cannot provide real AI answers/)).toBeTruthy();
    expect(screen.queryByText("AI connected")).toBeNull();
  });
});
