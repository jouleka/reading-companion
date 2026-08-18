import { useEffect, useMemo, useState } from "react";
import {
  api,
  type CredentialMetadata,
  type HostedProvider,
  type ProviderCapability,
  type ProviderSetting,
  type ProviderSettingsPayload,
} from "../api";

type Draft = {
  provider: HostedProvider;
  model: string;
  credential_id: string;
  base_url: string;
};

const capabilityLabel: Record<ProviderCapability, string> = {
  extraction: "Chapter extraction",
  synthesis: "Recaps and notes",
  embedding: "Semantic search",
  judge: "Spoiler review",
};

const statusLabel: Record<string, string> = {
  unchecked: "Not validated",
  ready: "Ready",
  offline: "Offline by choice",
  invalid_credentials: "Credentials rejected",
  unavailable_model: "Model unavailable",
  network_error: "Provider network unavailable",
  service_error: "Provider service unavailable",
};

function draftFor(
  capability: ProviderCapability,
  payload: ProviderSettingsPayload,
  credentials: CredentialMetadata[],
): Draft {
  const saved = payload.items.find((item) => item.capability === capability);
  const recommendation = payload.recommendations[capability];
  const provider = saved?.provider ?? recommendation.provider;
  return {
    provider,
    model: saved?.model ?? recommendation.model,
    credential_id: saved?.credential_id
      ?? credentials.find((item) => item.provider === provider)?.id
      ?? "",
    base_url: saved?.base_url ?? recommendation.base_url ?? "",
  };
}

function replaceSetting(items: ProviderSetting[], value: ProviderSetting): ProviderSetting[] {
  return [...items.filter((item) => item.capability !== value.capability), value];
}

export function ProviderSettings({ onClose }: { onClose: () => void }) {
  const [payload, setPayload] = useState<ProviderSettingsPayload | null>(null);
  const [credentials, setCredentials] = useState<CredentialMetadata[]>([]);
  const [drafts, setDrafts] = useState<Partial<Record<ProviderCapability, Draft>>>({});
  const [credentialProvider, setCredentialProvider] = useState<Exclude<HostedProvider, "offline">>(
    "openai-compatible",
  );
  const [secret, setSecret] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const refresh = async () => {
    const [nextPayload, nextCredentials] = await Promise.all([
      api.providerSettings(), api.credentials(),
    ]);
    setPayload(nextPayload);
    setCredentials(nextCredentials);
    setDrafts(Object.fromEntries(nextPayload.capabilities.map((capability) => [
      capability, draftFor(capability, nextPayload, nextCredentials),
    ])));
  };

  useEffect(() => {
    refresh().catch((error: unknown) => {
      setMessage(error instanceof Error ? error.message : "Settings could not be loaded.");
    });
  }, []);

  const credentialOptions = useMemo(() => credentials, [credentials]);
  const updateDraft = (capability: ProviderCapability, patch: Partial<Draft>) => {
    setDrafts((current) => ({
      ...current,
      [capability]: { ...current[capability]!, ...patch },
    }));
  };

  const addCredential = async () => {
    setBusy("credential");
    setMessage(null);
    try {
      await api.createCredential(credentialProvider, secret);
      setSecret("");
      await refresh();
      setMessage("Credential saved. Its secret cannot be read back.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Credential could not be saved.");
    } finally {
      setBusy(null);
    }
  };

  const removeCredential = async (id: string) => {
    setBusy(`delete:${id}`);
    setMessage(null);
    try {
      await api.deleteCredential(id);
      await refresh();
      setMessage("Credential deleted; settings that used it must be configured again.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Credential could not be deleted.");
    } finally {
      setBusy(null);
    }
  };

  const save = async (capability: ProviderCapability) => {
    const draft = drafts[capability]!;
    setBusy(`save:${capability}`);
    setMessage(null);
    try {
      const setting = await api.putProviderSetting(capability, {
        provider: draft.provider,
        model: draft.provider === "offline" ? "offline" : draft.model,
        credential_id: draft.provider === "offline" ? null : draft.credential_id || null,
        base_url: draft.provider === "offline" ? null : draft.base_url || null,
      });
      setPayload((current) => current && ({
        ...current, items: replaceSetting(current.items, setting),
      }));
      setMessage(`${capabilityLabel[capability]} saved. Validate it before new AI work starts.`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Provider setting could not be saved.");
    } finally {
      setBusy(null);
    }
  };

  const validate = async (capability: ProviderCapability) => {
    setBusy(`validate:${capability}`);
    setMessage(null);
    try {
      const result = await api.validateProviderSetting(capability);
      setPayload((current) => current && ({
        ...current, items: replaceSetting(current.items, result.setting),
      }));
      setMessage(statusLabel[result.code] ?? "Validation finished.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Provider validation could not run.");
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="provider-settings" aria-labelledby="provider-settings-title">
      <header className="provider-settings-head">
        <div>
          <p className="smallcaps">Hosted AI configuration</p>
          <h2 id="provider-settings-title">Your providers</h2>
        </div>
        <button type="button" className="plain" onClick={onClose}>Close settings</button>
      </header>

      {payload && (
        <div className="provider-notes">
          <p>{payload.offline_behavior}</p>
          <p>{payload.cost_ownership}</p>
          <p>Recommendations are shown as suggestions only. They are never saved over your choices.</p>
        </div>
      )}

      <fieldset className="provider-fieldset">
        <legend>Add a credential</legend>
        <label>
          Provider
          <select
            value={credentialProvider}
            onChange={(event) => setCredentialProvider(
              event.target.value as Exclude<HostedProvider, "offline">,
            )}
          >
            <option value="openai-compatible">OpenAI-compatible</option>
            <option value="anthropic">Anthropic</option>
          </select>
        </label>
        <label>
          Secret key
          <input
            type="password"
            autoComplete="off"
            value={secret}
            onChange={(event) => setSecret(event.target.value)}
          />
        </label>
        <button type="button" onClick={addCredential} disabled={!secret || busy !== null}>
          Save credential
        </button>
        <p className="quiet">The key is encrypted on submission and can never be displayed again.</p>
      </fieldset>

      {credentials.length > 0 && (
        <ul className="credential-list" aria-label="Saved credentials">
          {credentials.map((credential) => (
            <li key={credential.id}>
              <span>{credential.provider} · {credential.masked_label}</span>
              <button
                type="button"
                className="plain"
                disabled={busy !== null}
                onClick={() => removeCredential(credential.id)}
              >
                Delete
              </button>
            </li>
          ))}
        </ul>
      )}

      {payload?.capabilities.map((capability) => {
        const draft = drafts[capability];
        if (!draft) return null;
        const saved = payload.items.find((item) => item.capability === capability);
        const matchingCredentials = credentialOptions.filter(
          (item) => item.provider === draft.provider,
        );
        const validation = saved?.validation_error_code ?? saved?.validation_status ?? "unchecked";
        return (
          <fieldset key={capability} className="provider-fieldset capability-settings">
            <legend>{capabilityLabel[capability]}</legend>
            <label>
              Provider
              <select
                value={draft.provider}
                onChange={(event) => {
                  const provider = event.target.value as HostedProvider;
                  updateDraft(capability, {
                    provider,
                    model: provider === "offline" ? "offline" : draft.model,
                    credential_id: credentials.find((item) => item.provider === provider)?.id ?? "",
                    base_url: provider === "anthropic"
                      ? "https://api.anthropic.com/v1"
                      : provider === "openai-compatible" ? "https://api.openai.com/v1" : "",
                  });
                }}
              >
                <option value="openai-compatible">OpenAI-compatible</option>
                {capability !== "embedding" && <option value="anthropic">Anthropic</option>}
                <option value="offline">Offline</option>
              </select>
            </label>
            {draft.provider !== "offline" && (
              <>
                <label>
                  Model
                  <input
                    value={draft.model}
                    onChange={(event) => updateDraft(capability, { model: event.target.value })}
                  />
                </label>
                <label>
                  Credential
                  <select
                    value={draft.credential_id}
                    onChange={(event) => updateDraft(capability, {
                      credential_id: event.target.value,
                    })}
                  >
                    <option value="">Choose a credential</option>
                    {matchingCredentials.map((credential) => (
                      <option key={credential.id} value={credential.id}>
                        {credential.masked_label}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  Provider API URL
                  <input
                    type="url"
                    value={draft.base_url}
                    onChange={(event) => updateDraft(capability, { base_url: event.target.value })}
                  />
                </label>
              </>
            )}
            <div className="provider-actions">
              <button type="button" disabled={busy !== null} onClick={() => save(capability)}>
                Save
              </button>
              <button
                type="button"
                disabled={busy !== null || !saved}
                onClick={() => validate(capability)}
              >
                Validate
              </button>
              <span role="status">{statusLabel[validation] ?? validation}</span>
            </div>
          </fieldset>
        );
      })}

      {message && <p role="alert" className="provider-message">{message}</p>}
    </section>
  );
}
