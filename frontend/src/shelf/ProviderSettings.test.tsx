import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { StrictMode } from "react";
import userEvent from "@testing-library/user-event";
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
  extraction: {
    provider: "openai-compatible" as const,
    model: "gpt-4o-mini",
    base_url: "https://api.openai.com/v1",
  },
  synthesis: {
    provider: "openai-compatible" as const,
    model: "gpt-4o",
    base_url: "https://api.openai.com/v1",
  },
  embedding: {
    provider: "openai-compatible" as const,
    model: "text-embedding-3-small",
    base_url: "https://api.openai.com/v1",
  },
  judge: {
    provider: "openai-compatible" as const,
    model: "gpt-4o-mini",
    base_url: "https://api.openai.com/v1",
  },
};

const payload = {
  capabilities: ["extraction", "synthesis", "embedding", "judge"] as const,
  providers: ["openai-compatible", "anthropic", "offline"] as const,
  recommendations,
  recommendations_persisted: false as const,
  offline_behavior:
    "Without a validated provider, new AI processing stays offline; existing Codex memory remains available.",
  cost_ownership:
    "Provider usage is billed to the account behind the selected credential.",
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
    setting: {
      ...saved,
      validation_status: "invalid",
      validation_error_code: "invalid_credentials",
    },
  });
});

describe("Hosted provider settings", () => {
  test("explains offline behavior, cost ownership, and does not persist recommendations on load", async () => {
    render(<ProviderSettings onClose={() => {}} />);
    expect(
      await screen.findByText(/new AI processing stays offline/i),
    ).toBeTruthy();
    expect(screen.getByText(/usage is billed to the account/i)).toBeTruthy();
    expect(
      screen.getByText(/suggestions only.*never saved over your choices/i),
    ).toBeTruthy();
    expect(mocks.putProviderSetting).not.toHaveBeenCalled();
  });

  test("saves an explicit owner choice and reports the validation class", async () => {
    render(<ProviderSettings onClose={() => {}} />);
    const group = await screen.findByRole("group", {
      name: "Chapter extraction",
    });
    const model = within(group).getByRole("textbox", { name: "Model" });
    fireEvent.change(model, { target: { value: "chosen-model" } });
    fireEvent.click(within(group).getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(mocks.putProviderSetting).toHaveBeenCalledWith("extraction", {
        provider: "openai-compatible",
        model: "chosen-model",
        credential_id: credential.id,
        base_url: "https://api.openai.com/v1",
      }),
    );
    fireEvent.click(within(group).getByRole("button", { name: "Validate" }));
    expect(
      (await screen.findAllByText("Credentials rejected")).length,
    ).toBeGreaterThan(0);
  });

  test("submits a password field once and clears it after encrypted storage", async () => {
    render(<ProviderSettings onClose={() => {}} />);
    const secret = (await screen.findByLabelText(
      "Secret key",
    )) as HTMLInputElement;
    expect(secret.type).toBe("password");
    fireEvent.change(secret, { target: { value: "private-canary" } });
    fireEvent.click(screen.getByRole("button", { name: "Save credential" }));
    await waitFor(() =>
      expect(mocks.createCredential).toHaveBeenCalledWith(
        "openai-compatible",
        "private-canary",
      ),
    );
    await waitFor(() => expect(secret.value).toBe(""));
    expect(document.body.textContent).not.toContain("private-canary");
  });

  test("has no automated accessibility violations", async () => {
    const { baseElement } = render(<ProviderSettings onClose={() => {}} />);
    await screen.findByRole("group", { name: "Chapter extraction" });
    expect(await axeAA(baseElement)).toHaveNoViolations();
  });

  test("opens as a modal, locks the background, and restores focus when dismissed", async () => {
    const user = userEvent.setup();
    const opener = document.createElement("button");
    opener.textContent = "AI provider settings";
    document.body.append(opener);
    opener.focus();
    const onClose = vi.fn();
    const { unmount } = render(<ProviderSettings onClose={onClose} />);
    const dialog = screen.getByRole("dialog", { name: "Your providers" });
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Close settings" }),
    );
    expect(document.body.style.overflow).toBe("hidden");
    expect(opener.inert).toBe(true);
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledOnce();
    unmount();
    expect(document.activeElement).toBe(opener);
    expect(document.body.style.overflow).toBe("");
    expect(opener.inert).not.toBe(true);
    opener.remove();
  });

  test("does not validate saved settings while changed fields are still unsaved", async () => {
    mocks.providerSettings.mockResolvedValue({ ...payload, items: [saved] });
    render(<ProviderSettings onClose={() => {}} />);
    const group = await screen.findByRole("group", {
      name: "Chapter extraction",
    });
    const validate = within(group).getByRole("button", {
      name: "Validate",
    }) as HTMLButtonElement;
    expect(validate.disabled).toBe(false);
    fireEvent.change(within(group).getByRole("textbox", { name: "Model" }), {
      target: { value: "an-unsaved-model" },
    });
    expect(validate.disabled).toBe(true);
    expect(mocks.validateProviderSetting).not.toHaveBeenCalled();
  });

  test("hides modification forms after initial-load failure and retries settings and credentials together", async () => {
    const user = userEvent.setup();
    mocks.providerSettings.mockRejectedValueOnce(
      new Error("Your provider settings could not be loaded."),
    );
    render(<ProviderSettings onClose={() => {}} />);
    expect(screen.getByText("Loading your provider settings…")).toBeTruthy();
    expect(
      screen.queryByRole("group", { name: "Add a credential" }),
    ).toBeNull();
    expect(await screen.findByRole("alert")).toHaveProperty(
      "textContent",
      expect.stringContaining("could not be loaded"),
    );
    expect(screen.queryByLabelText("Secret key")).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Save credential" }),
    ).toBeNull();
    expect(mocks.createCredential).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Retry settings" }));
    expect(
      await screen.findByRole("group", { name: "Chapter extraction" }),
    ).toBeTruthy();
    expect(screen.getByLabelText("Secret key")).toBeTruthy();
    expect(mocks.providerSettings).toHaveBeenCalledTimes(2);
    expect(mocks.credentials).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  test("operation failures preserve usable drafts and do not become initial-load failures", async () => {
    const user = userEvent.setup();
    mocks.createCredential.mockRejectedValueOnce(
      new Error("The credential could not be saved."),
    );
    render(<ProviderSettings onClose={() => {}} />);
    const group = await screen.findByRole("group", {
      name: "Chapter extraction",
    });
    const model = within(group).getByRole("textbox", {
      name: "Model",
    }) as HTMLInputElement;
    fireEvent.change(model, { target: { value: "my-unsaved-model" } });
    await user.type(screen.getByLabelText("Secret key"), "private-key");
    await user.click(screen.getByRole("button", { name: "Save credential" }));
    expect(await screen.findByRole("alert")).toHaveProperty(
      "textContent",
      "The credential could not be saved.",
    );
    expect(model.value).toBe("my-unsaved-model");
    expect(
      screen.getByRole("group", { name: "Add a credential" }),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Retry settings" })).toBeNull();
  });

  test("a cancelled initial request cannot replace the current settings after StrictMode remount", async () => {
    let releaseOld!: (value: typeof payload) => void;
    mocks.providerSettings.mockReturnValueOnce(
      new Promise<typeof payload>((resolve) => {
        releaseOld = resolve;
      }),
    );
    render(
      <StrictMode>
        <ProviderSettings onClose={() => {}} />
      </StrictMode>,
    );
    const group = await screen.findByRole("group", {
      name: "Chapter extraction",
    });
    const model = within(group).getByRole("textbox", {
      name: "Model",
    }) as HTMLInputElement;
    expect(model.value).toBe(recommendations.extraction.model);
    await act(async () => {
      releaseOld({
        ...payload,
        recommendations: {
          ...recommendations,
          extraction: {
            ...recommendations.extraction,
            model: "stale-response-model",
          },
        },
      });
    });
    expect(model.value).toBe(recommendations.extraction.model);
  });
});
