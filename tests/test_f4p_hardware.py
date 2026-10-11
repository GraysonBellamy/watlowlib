"""Read-only smoke test against the live ISDC IR-301 (Series F4P) on the bench.

Deselected by default through the ``hardware`` marker. Run it with::

    pytest -m hardware tests/test_f4p_hardware.py

The port and address come from ``WATLOWLIB_F4P_PORT`` (default ``COM8``,
the DTECH USB-RS-485 adapter) and ``WATLOWLIB_F4P_ADDRESS`` (default
``10``). The controller is a live calibration source that can sit at
600 °C or more, so this test issues FC03 reads only: identify, the
process value, the set point, the output power, and a few status
registers. No parameter or set point is ever written.
"""

from __future__ import annotations

import os

import pytest

from watlowlib import F4P_PROFILE, ControllerFamily, Unit, open_device
from watlowlib.devices.models import DeviceHealth
from watlowlib.devices.profile import F4P_MODEL_CODE

pytestmark = pytest.mark.hardware

PORT = os.environ.get("WATLOWLIB_F4P_PORT", "COM8")
ADDRESS = int(os.environ.get("WATLOWLIB_F4P_ADDRESS", "10"))


@pytest.fixture
def anyio_backend() -> str:
    """One backend is enough for a bench smoke test."""
    return "asyncio"


@pytest.mark.anyio
async def test_f4p_identify_and_read_only_snapshot(anyio_backend: object) -> None:
    _ = anyio_backend
    ctl = await open_device(PORT, profile=F4P_PROFILE, address=ADDRESS)
    async with ctl:
        info = await ctl.identify()
        assert info.family is ControllerFamily.F4P
        assert info.hardware_id == F4P_MODEL_CODE
        assert info.health is DeviceHealth.OK
        assert info.serial_number is not None
        assert info.address == ADDRESS
        assert info.serial_settings.baudrate == 19200

        decimals = await ctl.read_parameter("input_1_decimal")
        assert isinstance(decimals.value, int)
        assert 0 <= decimals.value <= 3
        assert ctl.session.registry.resolve("process_value").scale == 10.0**-decimals.value

        unit = ctl.session.wire_temperature_unit()
        assert unit in (Unit.CELSIUS, Unit.FAHRENHEIT)

        pv = await ctl.read_pv()
        assert pv.value is not None
        assert pv.unit is unit
        assert -100.0 <= pv.value <= 3000.0

        sp = await ctl.read_setpoint()
        assert sp.value is not None
        assert sp.unit is unit
        assert -100.0 <= sp.value <= 3000.0

        power = await ctl.read_parameter("output_power")
        assert isinstance(power.value, float)
        assert -100.0 <= power.value <= 100.0

        for name in (
            "input_error",
            "alarm_1_status",
            "alarm_2_status",
            "auto_manual",
            "system_error_status",
            "show_temperature_unit",
        ):
            entry = await ctl.read_parameter(name)
            assert isinstance(entry.value, int), name
