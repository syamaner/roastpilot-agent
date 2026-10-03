/**
 * Read-only cold-characterisation view (`/cold-characterisation`).
 *
 * Renders only what the cold event stream delivers: the last retained display
 * observation, the event-connection status and two integer counters. It needs no
 * stored roast, has no controls, and makes no request other than the single event
 * stream opened by `useColdObservationStream`. The cold phase is shown through a
 * closed label table and never inferred. Heat, roast fan and cooling are commanded
 * state reported by MCP, not measurements; the main fan is never shown. The UTC
 * instant is shown verbatim, never parsed or reformatted.
 */

import { AppFrame } from "@/components/shared/AppFrame";
import { cn } from "@/lib/cn";
import {
  COLD_PHASE_LABEL,
  ROAST_FAN_OUTCOME_LABEL,
  type ColdObservation,
} from "@/lib/coldObservation";
import {
  useColdObservationStream,
  type ColdObservationStreamState,
  type ColdTransportStatus,
  type UseColdObservationStreamOptions,
} from "@/hooks/useColdObservationStream";
import { formatPercent, formatTempC } from "@/pages/dashboard/format";

/** The pinned retained-observation disclaimer (rendered exactly once). */
export const RETAINED_DISCLAIMER =
  "These values are the last retained display observation; they are not accepted or safe ticks. Heat, roast fan and cooling are commanded state reported by MCP, not measurements.";

/** The synthetic-harness banner copy. */
export const SYNTHETIC_BANNER = "Synthetic harness data — not an observation from any roaster";

const STATUS_LABEL: Record<ColdTransportStatus, string> = {
  connecting: "Connecting",
  connected: "Connected",
  reconnecting: "Reconnecting",
};

const STATUS_CLASS: Record<ColdTransportStatus, string> = {
  connecting: "text-muted-foreground",
  connected: "text-roast-nominal",
  reconnecting: "text-roast-caution",
};

const NONE = "—";

function roastFanText(observation: ColdObservation | null): string {
  if (observation === null) return NONE;
  const label = ROAST_FAN_OUTCOME_LABEL[observation.roast_fan_outcome];
  if (observation.roast_fan_percent === null) return label;
  return `${label}, ${formatPercent(observation.roast_fan_percent)}`;
}

function coolingText(cooling: boolean | null | undefined): string {
  if (cooling === true) return "On";
  if (cooling === false) return "Off";
  return NONE;
}

interface FieldProps {
  label: string;
  testId: string;
  value: string;
  breakAll?: boolean;
}

function Field({ label, testId, value, breakAll = false }: FieldProps): React.JSX.Element {
  return (
    <div className="flex flex-col gap-1 rounded-lg border border-border bg-card px-4 py-3">
      <dt className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{label}</dt>
      <dd
        className={cn("numeric text-lg font-semibold text-foreground", breakAll && "break-all")}
        data-testid={testId}
      >
        {value}
      </dd>
    </div>
  );
}

export interface ColdCharacterisationViewProps {
  state: ColdObservationStreamState;
  /** True only on the dev/test harness: shows the synthetic-data banner. */
  synthetic: boolean;
}

/** Presentational read-only view over one stream state. */
export function ColdCharacterisationView({
  state,
  synthetic,
}: ColdCharacterisationViewProps): React.JSX.Element {
  const { observation, status } = state;
  return (
    <AppFrame
      headerRight={
        <span
          className={cn("text-xs font-semibold uppercase tracking-wide", STATUS_CLASS[status])}
          data-testid="cold-transport"
          data-status={status}
        >
          Event connection: {STATUS_LABEL[status]}
        </span>
      }
    >
      <div className="mx-auto flex max-w-4xl flex-col gap-4" data-testid="cold-view">
        {synthetic && (
          <p
            className="rounded-lg border border-roast-caution px-4 py-3 text-sm font-semibold text-roast-caution"
            data-testid="cold-synthetic-banner"
          >
            {SYNTHETIC_BANNER}
          </p>
        )}

        <section className="rounded-lg border border-border bg-card p-4">
          <h1 className="text-lg font-semibold text-foreground">Cold characterisation</h1>
          <ul className="mt-2 flex list-disc flex-col gap-1 pl-5 text-sm text-muted-foreground">
            <li>This view is read-only and has no controls.</li>
            <li>The status in the header describes the event connection only.</li>
            <li data-testid="cold-disclaimer-retained">{RETAINED_DISCLAIMER}</li>
            <li>An independent operator emergency stop is required.</li>
          </ul>
        </section>

        <dl className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <div className="col-span-2 md:col-span-4">
            <Field
              label="Recorded at (UTC, as sent)"
              testId="cold-last-utc"
              value={observation?.recorded_at_utc ?? NONE}
              breakAll
            />
          </div>
          <Field
            label="Cold phase"
            testId="cold-phase"
            value={observation === null ? NONE : COLD_PHASE_LABEL[observation.cold_phase]}
          />
          <Field
            label="Bean temperature"
            testId="cold-bean"
            value={formatTempC(observation?.bean_temp_c)}
          />
          <Field
            label="Environment temperature"
            testId="cold-env"
            value={formatTempC(observation?.env_temp_c)}
          />
          <Field
            label="Heat (commanded)"
            testId="cold-heat"
            value={formatPercent(observation?.heat_percent)}
          />
          <Field label="Roast fan (commanded)" testId="cold-roast-fan" value={roastFanText(observation)} />
          <Field
            label="Cooling (commanded)"
            testId="cold-cooling"
            value={coolingText(observation?.cooling_on)}
          />
        </dl>

        {observation !== null && !observation.device_reported && (
          <p className="text-sm font-medium text-roast-caution" data-testid="cold-no-device">
            No device state in this observation
          </p>
        )}

        <dl className="flex gap-6 text-sm text-muted-foreground">
          <div className="flex gap-2">
            <dt>Frames shown</dt>
            <dd className="numeric font-semibold text-foreground" data-testid="cold-shown-count">
              {state.shownCount}
            </dd>
          </div>
          <div className="flex gap-2">
            <dt>Frames refused</dt>
            <dd className="numeric font-semibold text-foreground" data-testid="cold-refused-count">
              {state.refusedCount}
            </dd>
          </div>
        </dl>
      </div>
    </AppFrame>
  );
}

export interface ColdCharacterisationPageProps {
  /** Seam for the dev/test harness; the product route passes nothing. */
  streamOptions?: UseColdObservationStreamOptions;
  synthetic?: boolean;
}

/** The product route: the real hook connected to the presentational view. */
export function ColdCharacterisationPage({
  streamOptions,
  synthetic = false,
}: ColdCharacterisationPageProps = {}): React.JSX.Element {
  const state = useColdObservationStream(streamOptions);
  return <ColdCharacterisationView state={state} synthetic={synthetic} />;
}
