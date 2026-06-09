/**
 * Export downloads (E10-S5, ui-prompts Prompt C #6 / kickoff §2 detail row).
 *
 * Three download links — JSONL / CSV / summary JSON — built from
 * `api.logArtifactUrl`. The export manifest is server-owned: when it is absent or
 * `ready === false` the links are disabled (the artifacts don't exist yet), and
 * the manifest's `note` explains why. Nothing here calls MCP; the URLs resolve to
 * the agent's `GET /api/roasts/{id}/log/{artifact}` routes.
 */

import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import type { LogManifest } from "@/lib/types";

type Artifact = "jsonl" | "csv" | "summary";

const ARTIFACTS: { id: Artifact; label: string }[] = [
  { id: "jsonl", label: "JSONL" },
  { id: "csv", label: "CSV" },
  { id: "summary", label: "Summary JSON" },
];

export interface ExportOptionsProps {
  runId: string;
  manifest: LogManifest | null | undefined;
  className?: string;
}

export function ExportOptions({ runId, manifest, className }: ExportOptionsProps): React.JSX.Element {
  const ready = manifest?.ready === true;

  return (
    <div
      data-testid="export-options"
      data-ready={ready ? "true" : "false"}
      className={cn("flex flex-col gap-2 rounded-lg border border-border bg-card p-4", className)}
    >
      <h3 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Export</h3>
      <div className="flex flex-wrap gap-2">
        {ARTIFACTS.map((artifact) =>
          ready ? (
            <a
              key={artifact.id}
              data-testid={`export-${artifact.id}`}
              href={api.logArtifactUrl(runId, artifact.id)}
              download
              className="inline-flex items-center rounded-md border border-border bg-secondary px-3 py-1.5 text-sm font-medium hover:bg-secondary/70"
            >
              {artifact.label}
            </a>
          ) : (
            <span
              key={artifact.id}
              data-testid={`export-${artifact.id}`}
              data-disabled="true"
              aria-disabled
              className="inline-flex cursor-not-allowed items-center rounded-md border border-border px-3 py-1.5 text-sm font-medium text-muted-foreground opacity-50"
            >
              {artifact.label}
            </span>
          ),
        )}
      </div>
      {!ready && (
        <p className="text-xs text-muted-foreground" data-testid="export-note">
          {manifest?.note ?? "Exports are available once the roast log has been written."}
        </p>
      )}
    </div>
  );
}
