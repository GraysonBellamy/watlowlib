# `watlowlib.sync`

Sync facade over the async core. Every async method has a sync parity
routed through a `SyncPortal` (an `anyio.from_thread.BlockingPortal`).
See [Sync quickstart](../quickstart-sync.md).

## Public surface

::: watlowlib.sync

## Portal

::: watlowlib.sync.portal

## Controller

::: watlowlib.sync.controller

## Manager

::: watlowlib.sync.manager

## Recording

::: watlowlib.sync.recording

## Sinks

::: watlowlib.sync.sinks
