"""Synthetic diagnostic-collector boundary with no actuator surface."""

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
from roastpilot_agent.diagnostic_collector.interpretation_schema import (
    InterpretationInputError,
    InterpretationInputReason,
    InterpretationReport,
    SyntheticExpectations,
)
from roastpilot_agent.diagnostic_collector.interpreter import interpret_synthetic_bundle

__all__ = [
    "AdmittedBundle",
    "CollectorAdmissionError",
    "CollectorAdmissionReason",
    "CollectorAuthority",
    "CollectorStream",
    "CollectorStreamReason",
    "CollectorStreamState",
    "InterpretationInputError",
    "InterpretationInputReason",
    "InterpretationReport",
    "SyntheticExpectations",
    "interpret_synthetic_bundle",
    "read_synthetic_bundle",
]
