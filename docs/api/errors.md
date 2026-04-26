# `watlowlib.errors`

The typed exception hierarchy and `ErrorContext` dataclass. Every
public-facing error inherits from `WatlowError`; protocol-specific
errors carry per-protocol context (Std Bus class/member/instance,
Modbus register address / function code) on `error.context`. See
[Troubleshooting](../troubleshooting.md) for the common-error table.

## Public surface

::: watlowlib.errors
