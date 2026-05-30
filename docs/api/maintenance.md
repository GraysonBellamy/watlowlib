---
description: watlowlib.maintenance — persistent-write helpers change_baud, change_modbus_address, change_stdbus_address, change_protocol_mode; all require confirm=True.
---

# `watlowlib.maintenance`

Persistent-write helpers for one-shot device configuration:
`change_baud`, `change_modbus_address`, `change_stdbus_address`,
`change_protocol_mode`. All require `confirm=True`; protocol-mode flips
additionally gate on the SKU's comms-code support. See
[Safety](../safety.md) and [Troubleshooting](../troubleshooting.md).

## Public surface

::: watlowlib.maintenance
