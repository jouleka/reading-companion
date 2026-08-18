import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { axeAA } from "../test-a11y";

const mocks = vi.hoisted(() => ({
  providerSettings: vi.fn(),
  credentials: vi.fn(),
  createCredential: vi.fn(),
  deleteCredential: vi.fn(),
  putProviderSetting: vi.fn(),
  validateProviderSetting: vi.fn(),
}));

vi.mock("../api", () => ({ api: mocks }));

import { ProviderSettings } from "./ProviderSettings";

const credential = {
  id: "11111111-1111-4111-8111-111111111111",
  provider: "openai-compatible" as const,
  masked_label: "••••abcd",
  key_version: "v1",
  created_at: "2026-07-19T00:00:00Z",
  rotated_at: null,
  disabled_at: null,
};

const recommendations = {
  extraction: { provider: "openai-compatible" as const, model: "gpt-4o-mini", base_url: "https://api.openai.com/v1" },
  synthesis: { provider: "openai-compatible" as const, model: "gpt-4o", base_url: "https://api.openai.com/v1" },
  embedding: { provider: "openai-compatible" as const, model: "text-embedding-3-small", base_url: "https://api.openai.com/v1" },
  judge: { provider: "openai-compatible" as const, model: "gpt-4o-mini", base_url: "https://api.openai.com/v1" },
};

const payload = {
  capabilities: ["extraction", "synthesis", "embedding", "judge"] as const,
  providers: ["openai-compatible", "anthropic", "offline"] as const,
  recommendations,
  recommendations_persisted: false as const,
  offline_behavior: "Without a validated provider, new AI processing stays offline; existing Codex memory remains available.",
  cost_ownership: "Provider usage is billed to the account behind the selected credential.",
  items: [],
};

const saved = {
  id: "22222222-2222-4222-8222-222222222222",
  provider: "openai-compatible" as const,
  capability: "extraction" as const,
  credential_id: credential.id,
  model: "chosen-model",
  base_url: "https://api.openai.com/v1",
  enabled: true,
  validation_status: "unchecked" as const,
  validation_error_code: null,
  validated_at: null,
  created_at: "2026-07-19T00:00:00Z",
  updated_at: "2026-07-19T00:00:00Z",
};

beforeEach(() => {
  Object.values(mocks).forEach((mock) => mock.mockReset());
  mocks.providerSettings.mockResolvedValue(payload);
  mocks.credentials.mockResolvedValue([credential]);
  mocks.createCredential.mockResolvedValue(credential);
  mocks.deleteCredential.mockResolvedValue(undefined);
  mocks.putProviderSetting.mockResolvedValue(saved);
  mocks.validateProviderSetting.mockResolvedValue({
    status: "invalid",
    code: "invalid_credentials",
    setting: { ...saved, validation_status: "invalid", validation_error_code: "invalid_credentials" },
  });
});

describe("Hosted provider settings", () => {
  test("explains offline behavior, cost ownership, and does not persist recommendations on load", async () => {
    render(<ProviderSettings onClose={() => {}} />);
    expect(await screen.findByText(/new AI processing stays offline/i)).toBeTruthy();
    expect(screen.getByText(/usage is billed to the account/i)).toBeTruthy();
    expect(screen.getByText(/suggestions only.*never saved over your choices/i)).toBeTruthy();
    expect(mocks.putProviderSetting).not.toHaveBeenCalled();
  });

  test("saves an explicit owner choice and reports the validation class", async () => {
    render(<ProviderSettings onClose={() => {}} />);
    const group = await screen.findByRole("group", { name: "Chapter extraction" });
    const model = within(group).getByRole("textbox", { name: "Model" });
    fireEvent.change(model, { target: { value: "chosen-model" } });
    fireEvent.click(within(group).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mocks.putProviderSetting).toHaveBeenCalledWith("extraction", {
      provider: "openai-compatible",
      model: "chosen-model",
      credential_id: credential.id,
      base_url: "https://api.openai.com/v1",
    }));
    fireEvent.click(within(group).getByRole("button", { name: "Validate" }));
    expect((await screen.findAllByText("Credentials rejected")).length).toBeGreaterThan(0);
  });

  test("submits a password field once and clears it after encrypted storage", async () => {
    render(<ProviderSettings onClose={() => {}} />);
    const secret = screen.getByLabelText("Secret key") as HTMLInputElement;
    expect(secret.type).toBe("password");
    fireEvent.change(secret, { target: { value: "private-canary" } });
    fireEvent.click(screen.getByRole("button", { name: "Save credential" }));
    await waitFor(() => expect(mocks.createCredential).toHaveBeenCalledWith(
      "openai-compatible", "private-canary",
    ));
    await waitFor(() => expect(secret.value).toBe(""));
    expect(document.body.textContent).not.toContain("private-canary");
  });

  test("has no automated accessibility violations", async () => {
    const { container } = render(<ProviderSettings onClose={() => {}} />);
    await screen.findByRole("group", { name: "Chapter extraction" });
    expect(await axeAA(container)).toHaveNoViolations();
  });
});
