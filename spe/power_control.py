"""Power on/off control for SPE Expert amplifiers.

Power ON:  DTR hardware line toggle (no serial command exists for power on).
           Uses the *existing* serial_handler port — opening a second
           pyserial Serial() on the same tty toggles DTR/RTS as a side
           effect of pyserial's default termios configuration, which on
           the second power-action wedges the FTDI driver.
Power OFF: Serial command 0x0A (SWITCH OFF) per SPE Application Programmer's
           Guide Rev 1.1 for Expert 1.3K-FA / 1.5K-FA / 2K-FA.

Note: When DTR is held high the amplifier's front-panel power switch is
overridden ("POWER SWITCH HELD BY REMOTE" warning). The startup takes
3-4.5 seconds after DTR is raised.
"""

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from spe.config import SerialConfig

if TYPE_CHECKING:
    from spe.serial_handler import SerialHandler

logger = logging.getLogger(__name__)


class PowerController:
    """Controls SPE amplifier power via DTR (on) and serial command (off).

    Always works through the SerialHandler's already-open port. Never
    opens its own pyserial Serial() — doing so triggered FTDI wedging
    on the second power action because pyserial open touches termios
    and DTR/RTS as a side effect.
    """

    def __init__(self, serial_config: SerialConfig,
                 serial_handler: "SerialHandler"):
        self.serial_config = serial_config
        self.serial_handler = serial_handler
        self._lock = asyncio.Lock()

    async def power_on(self) -> bool:
        """Power on the SPE amplifier via DTR line toggle.

        Sequence (from OH2GEK power_spe_on.py):
          DTR=1 -> DTR=0 -> RTS=1 -> wait 1s -> DTR=1 -> RTS=0
        Startup takes 3-4.5 seconds after this sequence.
        """
        async with self._lock:
            self.serial_handler.note_power_action()
            return await asyncio.get_event_loop().run_in_executor(
                None, self._power_on_sync
            )

    async def power_off(self) -> bool:
        """Power off the SPE amplifier via SWITCH OFF command (0x0A).

        Per SPE Application Programmer's Guide, command 0x0A is the
        equivalent of pressing the front-panel OFF switch.
        """
        async with self._lock:
            self.serial_handler.note_power_action()
            return await asyncio.get_event_loop().run_in_executor(
                None, self._power_off_sync
            )

    def _power_on_sync(self) -> bool:
        # Operate on the SerialHandler's open port. Each .dtr/.rts
        # assignment is an ioctl on the already-open fd — no termios
        # reconfiguration, no second device handle, nothing for the
        # FTDI driver to choke on.
        port = self.serial_handler._port  # type: ignore[attr-defined]
        if port is None or not port.is_open:
            logger.error("Power ON failed: serial port not open")
            return False
        try:
            logger.info(f"Power ON: DTR/RTS sequence on {self.serial_config.port}")
            port.dtr = True
            port.dtr = False
            port.rts = True
            time.sleep(1)
            port.dtr = True
            port.rts = False
            logger.info("Power ON: DTR sequence complete (amp starting in 3-4s)")
            return True
        except Exception as e:
            logger.error(f"Power ON failed: {e}")
            return False

    def _power_off_sync(self) -> bool:
        # Queue the SWITCH OFF command through the existing handler so
        # it goes out on the same port everything else uses. No second
        # serial.Serial() open — that's what was wedging the FTDI on
        # the second power-off in a session.
        try:
            logger.info(f"Power OFF: queueing SWITCH OFF (0x0A) via serial_handler")
            self.serial_handler.send_command("power_off")
            return True
        except Exception as e:
            logger.error(f"Power OFF failed: {e}")
            return False
