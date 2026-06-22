/**
 * Detail title block (E10-S5, ui-prompts Prompt C #1).
 *
 * Bean/profile name, roast date, an outcome chip, and the headline stats (total
 * time, first crack time+temp, drop time+temp, development %). Every stat is
 * derived from the REST telemetry + timeline markers — nothing inferred. A faulted
 * roast shows its `fault_reason` rather than the post-roast stats.
 */

import { cn } from "@/lib/cn";
import type { RoastDetail, RoastOutcome } from "@/lib/types";
import { formatClock, formatDate, formatPercent1, formatTemp } from "./format";
import type { HeadlineStats } from "./traceModel";

const OUTCOME_META: Record<RoastOutcome, { label: string; tone: string }> = {
  completed: { label: "COMPLETED", tone: "border-roast-nominal/40 bg-roast-nominal/15 text-roast-nominal" },
  aborted: { label: "ABORTED", tone: "border-roast-caution/40 bg-roast-caution/15 text-roast-caution" },
  faulted: { label: "FAULTED", tone: "border-roast-fault/40 bg-roast-fault/15 text-roast-fault" },
};

export interface TitleBlockProps {
  detail: RoastDetail;
  stats: HeadlineStats;
  className?: string;
}

/** Capitalize a bean-species literal for display (e.g. "arabica" → "Arabica"). */
function speciesLabel(species: string): string {
  return species.charAt(0).toUpperCase() + species.slice(1);
}

export function TitleBlock({ detail, stats, className }: TitleBlockProps): React.JSX.Element {
  const { profile, outcome } = detail;
  const title = profile.bean_varietal
    ? `${profile.name} — ${profile.bean_varietal}`
    : profile.name;

  // Bean identity (#164), all read from the frozen profile (no client inference):
  // a "Country · Farm" line, a species/blend tag row, and the free-text
  // description. Each renders only when present, so pre-#164 profiles are
  // unaffected.
  const originParts = [profile.country, profile.farm].filter(
    (part): part is string => typeof part === "string" && part.length > 0,
  );
  const tags: string[] = [];
  if (profile.bean_species) tags.push(speciesLabel(profile.bean_species));
  if (profile.is_blend) tags.push("Blend");

  return (
    <div className={cn("flex flex-col gap-3", className)} data-testid="title-block">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
        {outcome && (
          <span
            data-testid="outcome-chip"
            data-outcome={outcome}
            className={cn(
              "inline-flex items-center rounded-md border px-2 py-0.5 text-xs font-semibold uppercase tracking-wide",
              OUTCOME_META[outcome].tone,
            )}
          >
            {OUTCOME_META[outcome].label}
          </span>
        )}
        <span className="text-sm text-muted-foreground" data-testid="roast-date">
          {formatDate(detail.completed_at_utc ?? detail.started_at_utc)}
        </span>
      </div>

      <p className="text-sm text-muted-foreground" data-testid="bean-origin">
        {profile.bean_origin}
      </p>

      {originParts.length > 0 && (
        <p className="text-sm text-muted-foreground" data-testid="bean-provenance">
          {originParts.join(" · ")}
        </p>
      )}

      {tags.length > 0 && (
        <div className="flex flex-wrap gap-2" data-testid="bean-tags">
          {tags.map((tag) => (
            <span
              key={tag}
              data-testid={`bean-tag-${tag.toLowerCase()}`}
              className="inline-flex items-center rounded-md border border-border bg-muted/40 px-2 py-0.5 text-xs font-medium uppercase tracking-wide text-muted-foreground"
            >
              {tag}
            </span>
          ))}
        </div>
      )}

      {profile.description && (
        <p className="text-sm text-muted-foreground" data-testid="bean-description">
          {profile.description}
        </p>
      )}

      {/* Product / source URL (#315): a clickable provenance link, read from the
          frozen profile. Opens in a new tab; renders nothing when absent (no
          broken anchor). The server validates it as a http(s) URL. */}
      {profile.source_url && (
        <a
          href={profile.source_url}
          target="_blank"
          rel="noopener noreferrer"
          data-testid="bean-source-url"
          className="text-sm text-roast-coffee underline underline-offset-2 hover:text-roast-coffee/80"
        >
          Product page
        </a>
      )}

      {detail.fault_reason ? (
        <p
          data-testid="fault-reason"
          className="rounded-md border border-roast-fault/40 bg-roast-fault/10 px-3 py-2 text-sm text-roast-fault"
        >
          {detail.fault_reason}
        </p>
      ) : (
        <dl className="flex flex-wrap gap-x-8 gap-y-2" data-testid="headline-stats">
          <Stat label="Total time" value={formatClock(stats.totalSeconds)} testId="stat-total" />
          <Stat
            label="First crack"
            value={`${formatClock(stats.firstCrackSeconds)} · ${formatTemp(stats.firstCrackTempC)}`}
            testId="stat-fc"
          />
          <Stat
            label="Drop"
            value={`${formatClock(stats.dropSeconds)} · ${formatTemp(stats.dropTempC)}`}
            testId="stat-drop"
          />
          <Stat label="Development" value={formatPercent1(stats.developmentPercent)} testId="stat-dev" />
        </dl>
      )}
    </div>
  );
}

interface StatProps {
  label: string;
  value: string;
  testId: string;
}

function Stat({ label, value, testId }: StatProps): React.JSX.Element {
  return (
    <div className="flex flex-col" data-testid={testId}>
      <dt className="text-xs uppercase tracking-wide text-muted-foreground">{label}</dt>
      <dd className="numeric text-lg font-medium">{value}</dd>
    </div>
  );
}
