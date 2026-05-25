---
description: watlowlib.config — Defaults for process-wide I/O timeout, drain idle, and default Standard Bus baud, threaded through open_device and WatlowManager.
---

# `watlowlib.config`

`Defaults` — process-wide defaults for I/O timeout, drain idle, and
default Std Bus baud. Most callers do not touch this module directly;
`open_device` and the `WatlowManager` thread the values through.

## Public surface

::: watlowlib.config
