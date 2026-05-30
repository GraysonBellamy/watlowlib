"""Workhorse READ_PARAMETER / WRITE_PARAMETER commands.

These two commands cover the 80% case: read or write any registry
parameter through the same code path on either protocol. The variant
pulls selector + encoding from the :class:`ParameterSpec` resolved at
encode time, so adding a new parameter to ``pm_parameters.json``
extends the surface with no command-layer changes.

See ``docs/design.md`` §5 / §5a.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from watlowlib.commands.base import Command, CommandContext
from watlowlib.devices.capability import SafetyTier
from watlowlib.devices.models import ParameterEntry
from watlowlib.errors import (
    ErrorContext,
    WatlowProtocolError,
    WatlowProtocolUnsupportedError,
    WatlowValidationError,
)
from watlowlib.protocol.modbus.codec import decode_words, encode_value_to_words
from watlowlib.protocol.modbus.ops import ModbusFn, ModbusOp
from watlowlib.protocol.modbus.tables import encoding_for
from watlowlib.protocol.stdbus.payload import (
    ErrorResponse,
    ReadResponse,
    WriteResponse,
    encode_read_request,
    encode_write_request,
    raise_for_error_code,
)
from watlowlib.protocol.stdbus.tlv import DataType

if TYPE_CHECKING:
    from watlowlib.protocol.stdbus.types import StdBusReply
    from watlowlib.registry.parameters import ParameterSpec

__all__ = [
    "READ_PARAMETER",
    "WRITE_PARAMETER",
    "ReadParameterRequest",
    "WriteParameterRequest",
]


@dataclass(frozen=True, slots=True)
class ReadParameterRequest:
    """Read one parameter at one instance."""

    name_or_id: str | int
    instance: int = 1


@dataclass(frozen=True, slots=True)
class WriteParameterRequest:
    """Write one parameter at one instance."""

    name_or_id: str | int
    value: float | int | str
    instance: int = 1


def _resolve(
    ctx: CommandContext,
    request: ReadParameterRequest | WriteParameterRequest,
) -> ParameterSpec:
    spec = ctx.registry.resolve(request.name_or_id)
    instance = request.instance
    if instance == 0:
        # Caller wants the default instance for this spec.
        instance = spec.default_instance
    ctx.registry.validate_instance(spec, instance)
    return spec


def _entry_from_payload(
    spec: ParameterSpec,
    instance: int,
    response: ReadResponse | WriteResponse,
    raw_frame: bytes,
) -> ParameterEntry:
    return ParameterEntry(
        spec=spec,
        instance=instance,
        value=response.value,
        raw=raw_frame,
    )


def _err_ctx(spec: ParameterSpec | None, request: object, ctx: CommandContext) -> ErrorContext:
    name_or_id = getattr(request, "name_or_id", None)
    instance = getattr(request, "instance", None)
    return ErrorContext(
        command_name=str(name_or_id) if name_or_id is not None else None,
        port=ctx.port or None,
        address=ctx.address or None,
        parameter_id=spec.parameter_id if spec else None,
        cls=spec.cls if spec else None,
        member=spec.member if spec else None,
        instance=instance,
    )


class _ReadParameterStdBus:
    """Std Bus variant for :data:`READ_PARAMETER`."""

    def encode(self, ctx: CommandContext, request: ReadParameterRequest) -> bytes:
        spec = _resolve(ctx, request)
        return encode_read_request(spec.parameter_id, instance=request.instance)

    def decode(self, reply: StdBusReply, ctx: CommandContext) -> ParameterEntry:
        # Re-resolve the spec from the response selector. The wire
        # response carries (cls, member, instance), which round-trips
        # back to a spec via parameter_id.
        payload = reply.payload
        if isinstance(payload, ErrorResponse):
            raise_for_error_code(
                payload.code,
                context=_err_ctx(None, ReadParameterRequest("<unknown>"), ctx),
            )
        if not isinstance(payload, ReadResponse):
            raise WatlowProtocolError(
                f"unexpected reply payload for read: {type(payload).__name__}",
                context=ErrorContext(port=ctx.port or None, address=ctx.address or None),
            )
        parameter_id = payload.cls * 1000 + payload.member
        try:
            spec = ctx.registry.resolve(parameter_id)
        except WatlowValidationError as exc:
            raise WatlowProtocolError(
                f"reply references unknown parameter id {parameter_id}",
                context=ErrorContext(
                    port=ctx.port or None,
                    address=ctx.address or None,
                    parameter_id=parameter_id,
                    cls=payload.cls,
                    member=payload.member,
                    instance=payload.instance,
                ),
            ) from exc
        return _entry_from_payload(spec, payload.instance, payload, reply.raw_frame)


class _WriteParameterStdBus:
    """Std Bus variant for :data:`WRITE_PARAMETER`."""

    def encode(self, ctx: CommandContext, request: WriteParameterRequest) -> bytes:
        spec = _resolve(ctx, request)
        ctx.registry.validate_value(spec, request.value)
        # STRING parameters accept str/bytes; numeric parameters
        # coerce in :func:`encode_value`.
        type_tag = int(spec.data_type)
        if spec.data_type is DataType.STRING:
            if not isinstance(request.value, str):
                raise WatlowValidationError(
                    f"parameter {spec.name!r} expects a string value, "
                    f"got {type(request.value).__name__}",
                )
            return encode_write_request(
                spec.parameter_id,
                request.value,
                instance=request.instance,
                type_tag=type_tag,
            )
        return encode_write_request(
            spec.parameter_id,
            request.value,
            instance=request.instance,
            type_tag=type_tag,
        )

    def decode(self, reply: StdBusReply, ctx: CommandContext) -> ParameterEntry:
        payload = reply.payload
        if isinstance(payload, ErrorResponse):
            raise_for_error_code(
                payload.code,
                context=ErrorContext(
                    port=ctx.port or None,
                    address=ctx.address or None,
                ),
            )
        if not isinstance(payload, WriteResponse):
            raise WatlowProtocolError(
                f"unexpected reply payload for write: {type(payload).__name__}",
                context=ErrorContext(port=ctx.port or None, address=ctx.address or None),
            )
        parameter_id = payload.cls * 1000 + payload.member
        try:
            spec = ctx.registry.resolve(parameter_id)
        except WatlowValidationError as exc:
            raise WatlowProtocolError(
                f"write reply references unknown parameter id {parameter_id}",
                context=ErrorContext(
                    port=ctx.port or None,
                    address=ctx.address or None,
                    parameter_id=parameter_id,
                ),
            ) from exc
        return _entry_from_payload(spec, payload.instance, payload, reply.raw_frame)


def _modbus_register_address(spec: ParameterSpec, instance: int) -> int:
    """Resolve the zero-based Modbus register address for an instance.

    PM single-loop devices use ``relative_addr`` directly. Multi-loop
    families need a per-family arithmetic offset that is not yet
    implemented — for now, instance > 1 surfaces as
    :class:`WatlowProtocolUnsupportedError` so the variant fails
    loudly rather than silently reading the wrong register.
    """
    if instance == spec.default_instance:
        return spec.relative_addr
    msg = (
        f"Modbus instance {instance} is not yet supported for parameter "
        f"{spec.name!r} (multi-loop Modbus instance arithmetic not implemented)"
    )
    raise WatlowProtocolUnsupportedError(msg)


def _modbus_err_ctx(spec: ParameterSpec, request: object, ctx: CommandContext) -> ErrorContext:
    return ErrorContext(
        command_name=str(getattr(request, "name_or_id", None) or "") or None,
        port=ctx.port or None,
        address=ctx.address or None,
        parameter_id=spec.parameter_id,
        register_address=spec.relative_addr,
        instance=getattr(request, "instance", None),
    )


class _ReadParameterModbus:
    """Modbus variant for :data:`READ_PARAMETER`."""

    def encode(self, ctx: CommandContext, request: ReadParameterRequest) -> ModbusOp:
        spec = _resolve(ctx, request)
        encoding = encoding_for(
            spec.data_type,
            word_order_override=spec.word_order,
            register_count_override=spec.register_count or None,
        )
        register = _modbus_register_address(spec, request.instance)
        return ModbusOp(
            fn=encoding.read_fn,
            address=register,
            count=encoding.register_count,
        )

    def decode(
        self,
        words: tuple[int, ...],
        ctx: CommandContext,
        request: ReadParameterRequest,
    ) -> ParameterEntry:
        spec = _resolve(ctx, request)
        encoding = encoding_for(
            spec.data_type,
            word_order_override=spec.word_order,
            register_count_override=spec.register_count or None,
        )
        try:
            value = decode_words(
                words,
                data_type=spec.data_type,
                word_order=encoding.word_order,
                byte_order=encoding.byte_order,
            )
        except WatlowProtocolError as exc:
            # Re-raise with parameter context attached.
            raise WatlowProtocolError(
                str(exc),
                context=_modbus_err_ctx(spec, request, ctx),
            ) from exc
        # Apply the engineering-unit scale (Modbus-only). Guard on
        # ``!= 1.0`` and never touch STRING: ``int * 1.0`` is a float,
        # so unconditional scaling would regress every unscaled integer
        # read to a float. Scaled rows (SD PV ÷1000, power ÷100) are
        # intentionally float.
        if spec.scale != 1.0 and isinstance(value, int | float):
            value = value * spec.scale
        # Pack words back to bytes for the Reading.raw payload — keeps
        # the cross-protocol Reading shape stable even though Modbus
        # never carries an "outer frame" the way Std Bus does.
        raw = b"".join(w.to_bytes(2, "big") for w in words)
        return ParameterEntry(spec=spec, instance=request.instance, value=value, raw=raw)


class _WriteParameterModbus:
    """Modbus variant for :data:`WRITE_PARAMETER`."""

    def encode(self, ctx: CommandContext, request: WriteParameterRequest) -> ModbusOp:
        spec = _resolve(ctx, request)
        # Validate in engineering units — ``range_min`` / ``range_max``
        # are parsed from the human-facing range string.
        ctx.registry.validate_value(spec, request.value)
        encoding = encoding_for(
            spec.data_type,
            word_order_override=spec.word_order,
            register_count_override=spec.register_count or None,
        )
        register = _modbus_register_address(spec, request.instance)
        # Scale engineering units back to the raw wire integer *after*
        # validation and *before* encoding. Guard on ``!= 1.0`` and skip
        # non-numeric (STRING) values. ``round`` keeps the nearest
        # integer count (SD setpoint 62.96 °F → 62960 raw).
        wire_value = request.value
        if spec.scale != 1.0 and isinstance(wire_value, int | float):
            wire_value = round(wire_value / spec.scale)
        try:
            words = encode_value_to_words(
                wire_value,
                data_type=spec.data_type,
                register_count=encoding.register_count,
                word_order=encoding.word_order,
                byte_order=encoding.byte_order,
            )
        except WatlowProtocolError as exc:
            raise WatlowValidationError(str(exc)) from exc
        if encoding.register_count == 1:
            return ModbusOp(
                fn=ModbusFn.WRITE_REGISTER,
                address=register,
                count=1,
                values=words,
            )
        return ModbusOp(
            fn=ModbusFn.WRITE_REGISTERS,
            address=register,
            count=encoding.register_count,
            values=words,
        )

    def decode(
        self,
        words: tuple[int, ...],
        ctx: CommandContext,
        request: WriteParameterRequest,
    ) -> ParameterEntry:
        # Modbus writes return no payload — the success of the call is
        # the acknowledgement. Echo the request value so callers see
        # the same ``ParameterEntry`` shape they get from a read.
        _ = words  # writes return ()
        spec = _resolve(ctx, request)
        return ParameterEntry(
            spec=spec,
            instance=request.instance,
            value=request.value,
            raw=b"",
        )


READ_PARAMETER: Command[ReadParameterRequest, ParameterEntry] = Command(
    name="read_parameter",
    stdbus=_ReadParameterStdBus(),
    modbus=_ReadParameterModbus(),
    safety=SafetyTier.READ_ONLY,
)

WRITE_PARAMETER: Command[WriteParameterRequest, ParameterEntry] = Command(
    name="write_parameter",
    stdbus=_WriteParameterStdBus(),
    modbus=_WriteParameterModbus(),
    safety=SafetyTier.PERSISTENT,
)
