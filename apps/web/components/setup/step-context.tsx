"use client";

import { Plus, Sparkles, Trash2 } from "lucide-react";

import { DocumentUpload } from "@/components/setup/document-upload";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { TagInput } from "@/components/ui/tag-input";
import { Textarea } from "@/components/ui/textarea";
import { Spinner } from "@/components/ui/spinner";
import type {
  AutofillField,
  AutofillFinding,
  AutofillSettings,
  Market,
  ProductContext,
} from "@/lib/api/projects";

/**
 * Step 1 — what we sell, and where.
 *
 * This is the text every research node is grounded on, so the fields are
 * prompts for specifics rather than empty boxes: what the product does, what it
 * costs, who it is for. Vague input here produces a vague report and no error
 * message anywhere.
 *
 * The document uploader below them is the same job at a different scale. The
 * boxes go into every node's prompt as configuration and have to stay short;
 * an uploaded file becomes citable evidence instead, which is how a brand with
 * a 30-page positioning deck gets to use it without putting it in front of
 * twenty-three model calls.
 */
export function StepContext({
  projectId,
  context,
  markets,
  onContextChange,
  onMarketsChange,
  disabled,
  autofill,
  findings,
  onAutofill,
  onAutofillOff,
  autofilling,
}: {
  projectId: string;
  context: ProductContext;
  markets: Market[];
  onContextChange: (context: ProductContext) => void;
  onMarketsChange: (markets: Market[]) => void;
  disabled: boolean;
  /** Which of these two fields this project has handed to the agent. */
  autofill: AutofillSettings;
  /** What the last run of it read, so the proposal can be judged, not just received. */
  findings: AutofillFinding[];
  onAutofill: (field: AutofillField) => void;
  onAutofillOff: (field: AutofillField) => void;
  /** The field currently being worked out, if any. */
  autofilling: AutofillField | null;
}) {
  const findingFor = (field: AutofillField) => findings.find((item) => item.field === field);
  const set = <K extends keyof ProductContext>(key: K, value: ProductContext[K]) =>
    onContextChange({ ...context, [key]: value });

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-1.5">
        <label htmlFor="summary" className="text-sm font-medium text-fg">
          What does this brand sell?
        </label>
        <Textarea
          id="summary"
          value={context.summary}
          disabled={disabled}
          rows={7}
          maxLength={8000}
          onChange={(event) => set("summary", event.target.value)}
          placeholder="Safety data sheet management software for EHS teams in manufacturing and construction. Replaces binders and spreadsheets with a searchable library, automatic SDS updates from suppliers, and compliance reporting for national regulators."
        />
        <p className="min-h-4 text-xs text-fg-muted">
          Write it the way you would explain it to a new salesperson. Every node in the run reads
          this.
        </p>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <TagInput
          label="Products"
          values={context.products}
          disabled={disabled}
          onChange={(values) => set("products", values)}
          placeholder="SDS Manager"
          hint="One per entry. Enter or comma to add."
        />
        <TagInput
          label="What makes us different"
          values={context.differentiators}
          disabled={disabled}
          onChange={(values) => set("differentiators", values)}
          placeholder="Automatic supplier SDS updates"
          hint="The claims the report will look for proof of."
        />
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <div className="flex flex-col gap-1.5">
          <label htmlFor="pricing" className="text-sm font-medium text-fg">
            Pricing
          </label>
          <Textarea
            id="pricing"
            rows={4}
            value={context.pricing}
            disabled={disabled}
            maxLength={4000}
            onChange={(event) => set("pricing", event.target.value)}
            placeholder="From €49/month per site. Free tier for under 50 sheets."
          />
          <p className="min-h-4 text-xs text-fg-muted">
            Used to judge whether a keyword can pay for itself.
          </p>
        </div>
        <div className="flex flex-col gap-1.5">
          <label htmlFor="icp" className="text-sm font-medium text-fg">
            Who we are trying to reach
          </label>
          <Textarea
            id="icp"
            rows={4}
            value={context.icp}
            disabled={disabled}
            maxLength={4000}
            onChange={(event) => set("icp", event.target.value)}
            placeholder="EHS managers at 50–500 employee manufacturers, usually the one person responsible for compliance."
          />
          <p className="min-h-4 text-xs text-fg-muted">
            Job titles and company shape, not demographics.
          </p>
        </div>
      </div>

      <DocumentUpload projectId={projectId} disabled={disabled} />

      <div className="space-y-2">
        <Field
          label="Site to crawl"
          value={context.site_url}
          disabled={disabled || autofilling === "site_url"}
          onChange={(event) => set("site_url", event.target.value)}
          placeholder="https://sdsmanager.com"
          hint="Optional. The crawler reads landing pages, CTAs and forms from here."
        />
        <AgentOption
          field="site_url"
          label="Let the agent find it"
          explanation="It follows the domain's redirects and keeps wherever they land — the www, the country path, the lot."
          on={autofill.site_url}
          busy={autofilling === "site_url"}
          disabled={disabled}
          finding={findingFor("site_url")}
          onAutofill={onAutofill}
          onAutofillOff={onAutofillOff}
        />
      </div>

      <div className="space-y-2">
        <Markets
          markets={markets}
          onChange={onMarketsChange}
          disabled={disabled || autofilling === "markets"}
        />
        <AgentOption
          field="markets"
          label="Let the agent work these out"
          explanation="It counts the countries in your CRM export and reads the language links your own site publishes. It proposes; nothing is invented, and you can edit or remove any of them."
          on={autofill.markets}
          busy={autofilling === "markets"}
          disabled={disabled}
          finding={findingFor("markets")}
          onAutofill={onAutofill}
          onAutofillOff={onAutofillOff}
        />
      </div>
    </div>
  );
}

/**
 * The offer to hand one field to the agent.
 *
 * A checkbox rather than a silent default, and it acts immediately rather than
 * promising to act at launch. Markets size the whole research run, so a scope
 * chosen without anyone seeing it is a scope nobody got the chance to disagree
 * with — the proposal belongs on the screen while it can still be corrected.
 */
function AgentOption({
  field,
  label,
  explanation,
  on,
  busy,
  disabled,
  finding,
  onAutofill,
  onAutofillOff,
}: {
  field: AutofillField;
  label: string;
  explanation: string;
  on: boolean;
  busy: boolean;
  disabled: boolean;
  finding: AutofillFinding | undefined;
  onAutofill: (field: AutofillField) => void;
  onAutofillOff: (field: AutofillField) => void;
}) {
  if (disabled) return null;
  return (
    <div className="rounded-[var(--radius)] border border-dashed px-3 py-2.5">
      <label className="flex cursor-pointer items-start gap-2.5 text-sm">
        <input
          type="checkbox"
          className="mt-0.5 size-4 shrink-0 accent-[var(--accent)]"
          checked={on}
          disabled={busy}
          onChange={(event) => (event.target.checked ? onAutofill(field) : onAutofillOff(field))}
        />
        <span className="min-w-0">
          <span className="flex items-center gap-1.5 font-medium text-fg">
            {busy ? <Spinner label="Working it out" /> : <Sparkles className="size-3.5" aria-hidden />}
            {label}
          </span>
          <span className="mt-0.5 block text-xs text-fg-muted">{explanation}</span>
        </span>
      </label>
      {finding ? (
        <p
          className={`mt-2 border-t pt-2 text-xs ${finding.found ? "text-fg-muted" : "text-status-failed"}`}
        >
          {finding.found ? "Read: " : "Nothing to read: "}
          {finding.source}
        </p>
      ) : null}
      {on && !busy ? (
        <button
          type="button"
          className="mt-2 text-xs text-fg-muted underline underline-offset-2 hover:text-fg"
          onClick={() => onAutofill(field)}
        >
          Work it out again
        </button>
      ) : null}
    </div>
  );
}

const LANGUAGES = ["en", "nb", "sv", "da", "fi", "de", "fr", "nl", "es", "it", "pl"];

function Markets({
  markets,
  onChange,
  disabled,
}: {
  markets: Market[];
  onChange: (markets: Market[]) => void;
  disabled: boolean;
}) {
  function update(index: number, patch: Partial<Market>) {
    onChange(markets.map((market, i) => (i === index ? { ...market, ...patch } : market)));
  }

  return (
    <fieldset className="space-y-3">
      <legend className="text-sm font-medium text-fg">Markets</legend>
      <p className="-mt-1 text-xs text-fg-muted">
        Keyword volume, CPC and seasonality are all per country, so a market is the unit the
        research is sized in.
      </p>

      {markets.length === 0 ? (
        <p className="rounded-[var(--radius)] border border-dashed px-3 py-4 text-sm text-fg-muted">
          No markets yet. Add the first country this campaign will run in.
        </p>
      ) : null}

      <ul className="space-y-2">
        {markets.map((market, index) => (
          <li key={index} className="flex flex-wrap items-center gap-2">
            <Input
              value={market.country}
              disabled={disabled}
              maxLength={2}
              aria-label={`Market ${index + 1} country`}
              onChange={(event) => update(index, { country: event.target.value.toUpperCase() })}
              placeholder="NO"
              className="w-20 font-mono uppercase"
            />
            <Select
              value={market.language}
              disabled={disabled}
              aria-label={`Market ${index + 1} language`}
              onChange={(event) => update(index, { language: event.target.value })}
              className="w-32"
            >
              {LANGUAGES.map((code) => (
                <option key={code} value={code}>
                  {code}
                </option>
              ))}
            </Select>
            <Input
              value={market.currency}
              disabled={disabled}
              maxLength={3}
              aria-label={`Market ${index + 1} currency`}
              onChange={(event) => update(index, { currency: event.target.value.toUpperCase() })}
              placeholder="NOK"
              className="w-24 font-mono uppercase"
            />
            {disabled ? null : (
              <Button
                type="button"
                variant="ghost"
                size="icon"
                aria-label={`Remove market ${market.country || index + 1}`}
                onClick={() => onChange(markets.filter((_, i) => i !== index))}
              >
                <Trash2 aria-hidden />
              </Button>
            )}
          </li>
        ))}
      </ul>

      {disabled ? null : (
        <Button
          type="button"
          variant="secondary"
          size="sm"
          onClick={() => onChange([...markets, { country: "", language: "en", currency: "" }])}
        >
          <Plus aria-hidden />
          Add market
        </Button>
      )}
    </fieldset>
  );
}
