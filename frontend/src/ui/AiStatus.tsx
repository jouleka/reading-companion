import {
  createContext,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { api, ApiError, type AiRuntime } from "../api";
import { Button } from "./Button";
import { Icon } from "./Icon";

type RuntimeState = {
  data: AiRuntime | null;
  failed: boolean;
  retry: () => void;
};
const RuntimeContext = createContext<RuntimeState | null>(null);

/** One owner-neutral local runtime request shared by the shelf, companion and Ask dialog. */
export function AiRuntimeProvider({
  children,
  enabled = true,
}: {
  children: ReactNode;
  enabled?: boolean;
}) {
  const [data, setData] = useState<AiRuntime | null>(null);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    let active = true;
    const timeout = window.setTimeout(() => controller.abort(), 5000);
    setFailed(false);
    void api
      .aiRuntime(controller.signal)
      .then(
        (value) => {
          if (active) setData(value);
        },
        (error: unknown) => {
          // Older/local versions and hosted mode may not expose the local runtime endpoint.
          if (active && !(error instanceof ApiError && error.status === 404))
            setFailed(true);
        },
      )
      .finally(() => window.clearTimeout(timeout));
    return () => {
      active = false;
      controller.abort();
      window.clearTimeout(timeout);
    };
  }, [attempt, enabled]);
  return (
    <RuntimeContext.Provider
      value={
        enabled
          ? { data, failed, retry: () => setAttempt((value) => value + 1) }
          : null
      }
    >
      {children}
    </RuntimeContext.Provider>
  );
}

export function AiStatus() {
  const runtime = useContext(RuntimeContext);
  if (!runtime || (!runtime.data && !runtime.failed)) return null;
  if (runtime.failed)
    return (
      <div className="ai-status" role="status">
        <strong>AI status unavailable</strong>
        <Button variant="ghost" onClick={runtime.retry}>
          Check connection
        </Button>
      </div>
    );
  const { data } = runtime;
  if (!data) return null;
  const demo = data.mode === "demo";
  const degraded = data.completion_status === "degraded";
  return (
    <div
      className="ai-status"
      data-mode={demo ? "demo" : degraded ? "unavailable" : "live"}
      role="status"
    >
      <strong>
        <Icon name="sparkle" size={15} />
        {demo
          ? "Preview mode · AI not connected"
          : degraded
            ? "AI connection unavailable"
            : data.completion_status === "ready"
              ? "AI connected"
              : "AI configured"}
      </strong>
      {demo ? (
        <>
          <p>
            Questions and recaps use an offline test model. This cannot provide
            real AI answers.
          </p>
          <details>
            <summary>Connect your AI provider</summary>
            <p>
              Add your provider key to the local .env configuration and restart
              the app. Your key stays on the server.
            </p>
          </details>
        </>
      ) : (
        <>
          <p>
            {data.models.synthesis}
            {degraded
              ? " · Check your provider configuration."
              : data.completion_status === "unchecked"
                ? " · Not yet checked by a successful request."
                : " · Answers still depend on the evidence in your completed pages."}
          </p>
          {data.embedding_mode === "demo" && (
            <p>
              Search is using test embeddings. Configure a real embedding
              provider before judging retrieval quality.
            </p>
          )}
          <Button variant="ghost" onClick={runtime.retry}>
            Check connection
          </Button>
        </>
      )}
    </div>
  );
}
