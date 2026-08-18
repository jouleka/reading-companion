import type { AskCost } from "../api";

export function ProviderCost({ cost }: { cost: AskCost }) {
  const usd = Number(cost.usd || 0);
  const summary = cost.calls.length === 0
    ? "Provider cost: $0 — no provider call was needed."
    : cost.pricing_known
      ? `Provider cost: $${usd.toFixed(6)} USD · ${cost.payer}.`
      : `Provider price unavailable — ${cost.payer}; check that account for the charge.`;
  return (
    <footer className="ask-cost">
      <p>{summary}</p>
      {cost.calls.length > 0 && (
        <p>
          {cost.calls.map((call) => `${call.provider} · ${call.model}`).join("; ")}
          {` · ${cost.input_tokens} input / ${cost.output_tokens} output tokens`}
        </p>
      )}
    </footer>
  );
}
