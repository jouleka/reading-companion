import { useEffect, useId, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Icon } from "../ui/Icon";
import { Button } from "../ui/Button";
import { useDialogMotion } from "../ui/useDialogMotion";
import { keepTabFocusInside } from "../reader/focusTrap";
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
    credential_id:
      saved?.credential_id ??
      credentials.find((item) => item.provider === provider)?.id ??
      "",
    base_url: saved?.base_url ?? recommendation.base_url ?? "",
  };
}

function replaceSetting(
  items: ProviderSetting[],
  value: ProviderSetting,
): ProviderSetting[] {
  return [
    ...items.filter((item) => item.capability !== value.capability),
    value,
  ];
}

export function ProviderSettings({ onClose }: { onClose: () => void }) {
  const { closing, requestClose } = useDialogMotion(onClose);
  const [payload, setPayload] = useState<ProviderSettingsPayload | null>(null);
  const [credentials, setCredentials] = useState<CredentialMetadata[]>([]);
  const [drafts, setDrafts] = useState<
    Partial<Record<ProviderCapability, Draft>>
  >({});
  const [credentialProvider, setCredentialProvider] =
    useState<Exclude<HostedProvider, "offline">>("openai-compatible");
  const [secret, setSecret] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [messageRole, setMessageRole] = useState<"status" | "alert">("status");
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const rootRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const triggerRef = useRef<HTMLElement | null>(
    document.activeElement as HTMLElement | null,
  );
  const onCloseRef = useRef(requestClose);
  onCloseRef.current = requestClose;
  const titleId = useId();
  const reportMessage = (value: string, error = false) => {
    setMessage(value);
    setMessageRole(error ? "alert" : "status");
  };

  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    const previousPadding = document.body.style.paddingRight;
    const scrollbarWidth =
      window.innerWidth - document.documentElement.clientWidth;
    if (document.documentElement.clientWidth > 0 && scrollbarWidth > 0) {
      const padding =
        Number.parseFloat(
          window.getComputedStyle(document.body).paddingRight,
        ) || 0;
      document.body.style.paddingRight = `${padding + scrollbarWidth}px`;
    }
    document.body.style.overflow = "hidden";
    const siblings = Array.from(document.body.children)
      .filter((element) => element !== rootRef.current)
      .map((element) => ({
        element: element as HTMLElement,
        inert: (element as HTMLElement).inert,
      }));
    for (const { element } of siblings) element.inert = true;
    closeRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onCloseRef.current();
      } else keepTabFocusInside(rootRef.current, event);
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
      document.body.style.paddingRight = previousPadding;
      for (const { element, inert } of siblings) element.inert = inert;
      const trigger = triggerRef.current;
      if (trigger && document.contains(trigger)) trigger.focus();
    };
  }, []);

  const refresh = async (preserveDrafts = false, isCurrent = () => true) => {
    const [nextPayload, nextCredentials] = await Promise.all([
      api.providerSettings(),
      api.credentials(),
    ]);
    if (!isCurrent()) return;
    setPayload(nextPayload);
    setCredentials(nextCredentials);
    setDrafts((current) =>
      Object.fromEntries(
        nextPayload.capabilities.map((capability) => {
          const existing = preserveDrafts ? current[capability] : undefined;
          const draft = existing
            ? {
                ...existing,
                credential_id: nextCredentials.some(
                  (item) => item.id === existing.credential_id,
                )
                  ? existing.credential_id
                  : "",
              }
            : draftFor(capability, nextPayload, nextCredentials);
          return [capability, draft];
        }),
      ),
    );
  };

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setLoadError(null);
    void refresh(false, () => !cancelled)
      .catch((error: unknown) => {
        if (!cancelled)
          setLoadError(
            error instanceof Error
              ? error.message
              : "Settings could not be loaded.",
          );
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [loadAttempt]);

  const credentialOptions = useMemo(() => credentials, [credentials]);
  const updateDraft = (
    capability: ProviderCapability,
    patch: Partial<Draft>,
  ) => {
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
      await refresh(true);
      reportMessage("Credential saved. Its secret cannot be read back.");
    } catch (error) {
      reportMessage(
        error instanceof Error
          ? error.message
          : "Credential could not be saved.",
        true,
      );
    } finally {
      setBusy(null);
    }
  };

  const removeCredential = async (id: string) => {
    setBusy(`delete:${id}`);
    setMessage(null);
    try {
      await api.deleteCredential(id);
      await refresh(true);
      reportMessage(
        "Credential deleted; settings that used it must be configured again.",
      );
    } catch (error) {
      reportMessage(
        error instanceof Error
          ? error.message
          : "Credential could not be deleted.",
        true,
      );
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
        credential_id:
          draft.provider === "offline" ? null : draft.credential_id || null,
        base_url: draft.provider === "offline" ? null : draft.base_url || null,
      });
      setPayload(
        (current) =>
          current && {
            ...current,
            items: replaceSetting(current.items, setting),
          },
      );
      reportMessage(
        `${capabilityLabel[capability]} saved. Validate it before new AI work starts.`,
      );
    } catch (error) {
      reportMessage(
        error instanceof Error
          ? error.message
          : "Provider setting could not be saved.",
        true,
      );
    } finally {
      setBusy(null);
    }
  };

  const validate = async (capability: ProviderCapability) => {
    setBusy(`validate:${capability}`);
    setMessage(null);
    try {
      const result = await api.validateProviderSetting(capability);
      setPayload(
        (current) =>
          current && {
            ...current,
            items: replaceSetting(current.items, result.setting),
          },
      );
      reportMessage(statusLabel[result.code] ?? "Validation finished.");
    } catch (error) {
      reportMessage(
        error instanceof Error
          ? error.message
          : "Provider validation could not run.",
        true,
      );
    } finally {
      setBusy(null);
    }
  };

  return createPortal(
    <div
      className="provider-settings-scrim"
      data-closing={closing || undefined}
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      ref={rootRef}
      tabIndex={-1}
    >
      <section
        className="provider-settings"
        aria-labelledby={titleId}
        aria-busy={loading || busy !== null}
      >
        <header className="provider-settings-head">
          <div>
            <p className="settings-eyebrow">AI configuration</p>
            <h2 id={titleId}>Your providers</h2>
          </div>
          <Button
            ref={closeRef}
            variant="ghost"
            className="icon-button"
            aria-label="Close settings"
            onClick={requestClose}
          >
            <Icon name="close" />
          </Button>
        </header>

        {payload && (
          <div className="provider-notes">
            <p>{payload.offline_behavior}</p>
            <p>{payload.cost_ownership}</p>
            <p>
              Recommendations are shown as suggestions only. They are never
              saved over your choices.
            </p>
          </div>
        )}
        {!payload && loading && (
          <p className="settings-loading" role="status">
            Loading your provider settings…
          </p>
        )}
        {!payload && loadError && (
          <div
            className="notice notice--error provider-load-error"
            role="alert"
          >
            <span>{loadError}</span>
            <Button
              icon="refresh"
              onClick={() => setLoadAttempt((value) => value + 1)}
            >
              Retry settings
            </Button>
          </div>
        )}

        {payload && (
          <>
            <fieldset className="provider-fieldset">
              <legend>Add a credential</legend>
              <label>
                Provider
                <select
                  value={credentialProvider}
                  onChange={(event) =>
                    setCredentialProvider(
                      event.target.value as Exclude<HostedProvider, "offline">,
                    )
                  }
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
              <Button
                variant="primary"
                onClick={addCredential}
                disabled={!secret || busy !== null}
              >
                {busy === "credential"
                  ? "Saving credential…"
                  : "Save credential"}
              </Button>
              <p className="quiet">
                The key is encrypted on submission and can never be displayed
                again.
              </p>
            </fieldset>

            {credentials.length > 0 && (
              <ul className="credential-list" aria-label="Saved credentials">
                {credentials.map((credential) => (
                  <li key={credential.id}>
                    <span>
                      {credential.provider} · {credential.masked_label}
                    </span>
                    <Button
                      variant="ghost"
                      disabled={busy !== null}
                      onClick={() => removeCredential(credential.id)}
                    >
                      Delete
                    </Button>
                  </li>
                ))}
              </ul>
            )}

            {payload?.capabilities.map((capability) => {
              const draft = drafts[capability];
              if (!draft) return null;
              const saved = payload.items.find(
                (item) => item.capability === capability,
              );
              const matchingCredentials = credentialOptions.filter(
                (item) => item.provider === draft.provider,
              );
              const validation =
                saved?.validation_error_code ??
                saved?.validation_status ??
                "unchecked";
              const dirty =
                !saved ||
                draft.provider !== saved.provider ||
                (draft.provider !== "offline" &&
                  (draft.model !== saved.model ||
                    (draft.credential_id || null) !== saved.credential_id ||
                    (draft.base_url || null) !== saved.base_url));
              return (
                <fieldset
                  key={capability}
                  className="provider-fieldset capability-settings"
                >
                  <legend>{capabilityLabel[capability]}</legend>
                  <label>
                    Provider
                    <select
                      value={draft.provider}
                      onChange={(event) => {
                        const provider = event.target.value as HostedProvider;
                        updateDraft(capability, {
                          provider,
                          model:
                            provider === "offline" ? "offline" : draft.model,
                          credential_id:
                            credentials.find(
                              (item) => item.provider === provider,
                            )?.id ?? "",
                          base_url:
                            provider === "anthropic"
                              ? "https://api.anthropic.com/v1"
                              : provider === "openai-compatible"
                                ? "https://api.openai.com/v1"
                                : "",
                        });
                      }}
                    >
                      <option value="openai-compatible">
                        OpenAI-compatible
                      </option>
                      {capability !== "embedding" && (
                        <option value="anthropic">Anthropic</option>
                      )}
                      <option value="offline">Offline</option>
                    </select>
                  </label>
                  {draft.provider !== "offline" && (
                    <>
                      <label>
                        Model
                        <input
                          value={draft.model}
                          onChange={(event) =>
                            updateDraft(capability, {
                              model: event.target.value,
                            })
                          }
                        />
                      </label>
                      <label>
                        Credential
                        <select
                          value={draft.credential_id}
                          onChange={(event) =>
                            updateDraft(capability, {
                              credential_id: event.target.value,
                            })
                          }
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
                          onChange={(event) =>
                            updateDraft(capability, {
                              base_url: event.target.value,
                            })
                          }
                        />
                      </label>
                    </>
                  )}
                  <div className="provider-actions">
                    <Button
                      variant="primary"
                      disabled={
                        busy !== null ||
                        (draft.provider !== "offline" && !draft.model.trim())
                      }
                      onClick={() => save(capability)}
                    >
                      {busy === `save:${capability}` ? "Saving…" : "Save"}
                    </Button>
                    <Button
                      disabled={busy !== null || !saved || dirty}
                      onClick={() => validate(capability)}
                    >
                      {busy === `validate:${capability}`
                        ? "Validating…"
                        : "Validate"}
                    </Button>
                    <span role="status">
                      {dirty
                        ? "Save changes before validating"
                        : (statusLabel[validation] ?? validation)}
                    </span>
                  </div>
                </fieldset>
              );
            })}
          </>
        )}

        {message && (
          <p role={messageRole} className="provider-message">
            {message}
          </p>
        )}
      </section>
    </div>,
    document.body,
  );
}
