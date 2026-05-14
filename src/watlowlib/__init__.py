"""watlowlib — Python library for Watlow temperature controllers.

Supports both wire protocols Watlow controllers expose:

- **Standard Bus**: BACnet MS/TP outer framing with a small Watlow
  attribute service inside.
- **Modbus RTU**: via the in-house ``anymodbus`` package.

The public API is semantic and protocol-neutral — a caller asks for
``read_pv()``, ``set_setpoint()``, ``read_parameter()``; the session
dispatches the Standard Bus or Modbus variant selected at open time.
Both protocols decode into the same frozen ``Reading`` /
``ParameterEntry`` / ``DeviceInfo`` models.

Core API is ``async`` (built on ``anyio``); :mod:`watlowlib.sync`
provides a blocking facade for scripts, notebooks, and REPL use.

See ``docs/design.md`` for the architectural design.
"""

from __future__ import annotations

from watlowlib.devices import (
    DEFAULT_DISCOVERY_ADDRESSES,
    DEFAULT_DISCOVERY_BAUDRATES,
    DEFAULT_DISCOVERY_PROTOCOLS,
    AlarmState,
    Availability,
    Capability,
    Controller,
    ControllerFamily,
    ControllerLoop,
    DeviceInfo,
    FindResult,
    LoopState,
    ParameterEntry,
    PartNumber,
    Reading,
    SafetyTier,
    Session,
    classify_family,
    find_devices,
    open_controller,
    open_device,
)
from watlowlib.errors import (
    ErrorContext,
    WatlowCapabilityError,
    WatlowCapabilityWarning,
    WatlowConfigurationError,
    WatlowConfirmationRequiredError,
    WatlowConnectionError,
    WatlowError,
    WatlowFirmwareError,
    WatlowFrameError,
    WatlowModbusError,
    WatlowModbusIllegalDataAddressError,
    WatlowModbusIllegalDataValueError,
    WatlowModbusIllegalFunctionError,
    WatlowModbusSlaveFailureError,
    WatlowModbusTimeoutError,
    WatlowNoSuchAttributeError,
    WatlowNoSuchInstanceError,
    WatlowNoSuchObjectError,
    WatlowProtocolError,
    WatlowProtocolUnsupportedError,
    WatlowSinkDependencyError,
    WatlowSinkError,
    WatlowSinkSchemaError,
    WatlowSinkWriteError,
    WatlowTimeoutError,
    WatlowTransportError,
    WatlowValidationError,
)
from watlowlib.firmware import FirmwareVersion
from watlowlib.manager import DeviceResult, ErrorPolicy, WatlowManager
from watlowlib.protocol import ProtocolClient, ProtocolKind
from watlowlib.registry import (
    PARAMETERS,
    ParameterRegistry,
    ParameterSpec,
    RwesFlag,
    Unit,
    UnitKind,
)
from watlowlib.sinks import (
    CsvSink,
    InMemorySink,
    JsonlSink,
    ParquetSink,
    PostgresConfig,
    PostgresSink,
    SampleSink,
    SqliteSink,
    pipe,
    sample_to_row,
)
from watlowlib.streaming import (
    AcquisitionSummary,
    OverflowPolicy,
    PollSource,
    Sample,
    record,
)
from watlowlib.transport import (
    FakeTransport,
    SerialSettings,
    SerialTransport,
    Transport,
)
from watlowlib.version import __version__

__all__ = [
    "DEFAULT_DISCOVERY_ADDRESSES",
    "DEFAULT_DISCOVERY_BAUDRATES",
    "DEFAULT_DISCOVERY_PROTOCOLS",
    "PARAMETERS",
    "AcquisitionSummary",
    "AlarmState",
    "Availability",
    "Capability",
    "Controller",
    "ControllerFamily",
    "ControllerLoop",
    "CsvSink",
    "DeviceInfo",
    "DeviceResult",
    "ErrorContext",
    "ErrorPolicy",
    "FakeTransport",
    "FindResult",
    "FirmwareVersion",
    "InMemorySink",
    "JsonlSink",
    "LoopState",
    "OverflowPolicy",
    "ParameterEntry",
    "ParameterRegistry",
    "ParameterSpec",
    "ParquetSink",
    "PartNumber",
    "PollSource",
    "PostgresConfig",
    "PostgresSink",
    "ProtocolClient",
    "ProtocolKind",
    "Reading",
    "RwesFlag",
    "SafetyTier",
    "Sample",
    "SampleSink",
    "SerialSettings",
    "SerialTransport",
    "Session",
    "SqliteSink",
    "Transport",
    "Unit",
    "UnitKind",
    "WatlowCapabilityError",
    "WatlowCapabilityWarning",
    "WatlowConfigurationError",
    "WatlowConfirmationRequiredError",
    "WatlowConnectionError",
    "WatlowError",
    "WatlowFirmwareError",
    "WatlowFrameError",
    "WatlowManager",
    "WatlowModbusError",
    "WatlowModbusIllegalDataAddressError",
    "WatlowModbusIllegalDataValueError",
    "WatlowModbusIllegalFunctionError",
    "WatlowModbusSlaveFailureError",
    "WatlowModbusTimeoutError",
    "WatlowNoSuchAttributeError",
    "WatlowNoSuchInstanceError",
    "WatlowNoSuchObjectError",
    "WatlowProtocolError",
    "WatlowProtocolUnsupportedError",
    "WatlowSinkDependencyError",
    "WatlowSinkError",
    "WatlowSinkSchemaError",
    "WatlowSinkWriteError",
    "WatlowTimeoutError",
    "WatlowTransportError",
    "WatlowValidationError",
    "__version__",
    "classify_family",
    "find_devices",
    "open_controller",
    "open_device",
    "pipe",
    "record",
    "sample_to_row",
]
