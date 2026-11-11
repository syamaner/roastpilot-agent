"""Synthetic diagnostic-collector admission boundary with no actuator surface."""

from roastpilot_agent.diagnostic_collector.bundle_reader import read_synthetic_bundle
from roastpilot_agent.diagnostic_collector.bundle_schema import (
    AdmittedBundle,
    CollectorAdmissionError,
    CollectorAdmissionReason,
    CollectorAuthority,
    CollectorStream,
    CollectorStreamReason,
    CollectorStreamState,
)

__all__ = [
    "AdmittedBundle",
    "CollectorAdmissionError",
    "CollectorAdmissionReason",
    "CollectorAuthority",
    "CollectorStream",
    "CollectorStreamReason",
    "CollectorStreamState",
    "read_synthetic_bundle",
]
