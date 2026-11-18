import {
  HUMAN_COMMAND_DISPOSITIONS,
  HUMAN_ENDPOINT_DISPOSITIONS,
  type HumanProtectionProjection,
} from "./types";

function isNullableMember<T extends string>(
  value: unknown,
  members: readonly T[],
): value is T | null {
  return value === null || members.some((member) => value === member);
}

export function hasHumanProtection(
  data: HumanProtectionProjection | null | undefined,
): data is HumanProtectionProjection {
  return data != null && Object.values(data).some((field) => field !== null);
}

/** Validate the complete projection. All nulls is an update; absent fields are not. */
export function readHumanProtection(
  data: Partial<Record<keyof HumanProtectionProjection, unknown>>,
): HumanProtectionProjection | undefined {
  const {
    human_endpoint,
    human_command_disposition,
    physical_intervention_required,
    emergency_fallback_disclosure,
  } = data;
  if (
    !isNullableMember(human_endpoint, HUMAN_ENDPOINT_DISPOSITIONS) ||
    !isNullableMember(human_command_disposition, HUMAN_COMMAND_DISPOSITIONS) ||
    (physical_intervention_required !== null && typeof physical_intervention_required !== "boolean") ||
    (emergency_fallback_disclosure !== null && typeof emergency_fallback_disclosure !== "string")
  ) {
    return undefined;
  }
  return {
    human_endpoint,
    human_command_disposition,
    physical_intervention_required,
    emergency_fallback_disclosure,
  };
}
