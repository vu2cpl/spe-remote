#!/usr/bin/env python3
"""SPE Amplifier Remote Control Server.

Modernized Python 3 port of the OH2GEK SPE remote control.
Communicates with SPE Expert amplifiers via serial and serves
a web-based remote control interface over WebSocket.
"""

import asyncio
import json
import logging
import signal
import sys
import time
from pathlib import Path

import tornado.ioloop

from spe.config import load_config
from spe.app import make_app
from spe.serial_handler import SerialHandler
from spe.power_control import PowerController
from spe.websocket_handler import AmplifierWebSocket
from spe.flex_controller import FlexController
from spe.tune_orchestrator import TuneOrchestrator


async def presence_heartbeat_loop(
    serial_handler: SerialHandler,
    interval: float,
    amp_alive_threshold: float,
) -> None:
    """Periodically broadcast a presence heartbeat to all WebSocket clients.

    Distinct from :attr:`PollingConfig.heartbeat`, which forces a state
    re-broadcast on the existing channel. This loop emits a separate
    ``{"heartbeat": True, "serial": "up"|"down", ...}`` message at a
    short, fixed cadence regardless of whether amp serial frames are
    flowing.

    ``serial`` reports **amp liveness**, not USB-link state. The FTDI
    cable stays USB-connected to the Pi even when the amp's CPU is dead,
    so ``serial_handler.connected`` would lie. Instead, ``serial: "up"``
    iff a CSV state frame OR an RCU display frame has been parsed within
    ``amp_alive_threshold`` seconds. CSV alone is insufficient because
    the amp slows CSV emission in STANDBY below the heartbeat threshold,
    which would otherwise produce false serial:"down" → POWERED OFF
    banners on clients while the amp is fine.

    Two consequences clients depend on:

    1. They learn within ``interval + amp_alive_threshold`` seconds that
       the amp went off — the serial-down transition no longer requires
       a fresh WS connect + snapshot to see.
    2. They see *something* flowing every ``interval`` seconds, which
       prevents heartbeat-based client reconnect loops (e.g. MacExpert
       reconnecting every 5 s when no msgs arrive).
    """
    logger = logging.getLogger("spe.heartbeat")
    while True:
        try:
            await asyncio.sleep(interval)
            alive = min(
                serial_handler.last_state_age,
                serial_handler.last_rcu_age,
            ) < amp_alive_threshold
            msg = json.dumps({
                "heartbeat": True,
                "serial": "up" if alive else "down",
                "ts": time.time(),
                "clients": len(AmplifierWebSocket.clients),
            })
            AmplifierWebSocket.broadcast_raw(msg)
        except asyncio.CancelledError:
            logger.info("presence_heartbeat_loop cancelled")
            break
        except Exception:
            logger.exception("presence_heartbeat_loop error; continuing")


def main() -> None:
    # Distinguish "user passed a path" from "no arg, fall back to default":
    # if a path is given explicitly it must exist, so a typo or copy-pasted
    # placeholder fails loudly instead of silently running on defaults.
    # Bare invocation with no arg keeps the original tolerant behaviour
    # (load_config logs a warning and uses defaults if config.yaml is absent).
    if len(sys.argv) > 1:
        config_path = sys.argv[1]
        if not Path(config_path).exists():
            print(
                f"error: config file {config_path!r} not found",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        config_path = "config.yaml"
    config = load_config(config_path)

    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logger = logging.getLogger("spe")

    serial_handler = SerialHandler(
        serial_config=config.serial,
        polling_config=config.polling,
        on_state_update=AmplifierWebSocket.broadcast_state,
        on_rcu_frame=AmplifierWebSocket.broadcast_rcu_frame,
        temperature_unit=config.amp.temperature_unit,
    )

    power_controller = PowerController(
        serial_config=config.serial,
        serial_handler=serial_handler,
    )

    # Optional Flex 6000 control — Phase 2 of the band-sweep work.
    # When flex.enabled is true, create a FlexController + tune
    # orchestrator that can drive an ATU tune cycle via the (SPE TUNE
    # keycode + Flex carrier) combination.
    #
    # On-demand lifecycle: the controller does NOT open the SmartSDR TCP
    # session at startup. It connects when a client opens its Sweep menu
    # (the flex_connect WS command) or, as a safety net, lazily at the
    # start of a tune cycle, and disconnects when the cycle is over.
    # Host resolution (static flex.host vs. UDP discovery) is deferred
    # into FlexController.connect() too, so the radio may be powered off
    # at startup and still be found later. Disabled by default; spe-remote
    # behaves exactly as before when the section is omitted from config.
    flex_controller = None
    tune_orchestrator = None
    if config.flex.enabled:
        flex_controller = FlexController(
            config.flex,
            on_status=AmplifierWebSocket.broadcast_tune_event,
        )
        tune_orchestrator = TuneOrchestrator(
            serial_handler=serial_handler,
            flex_controller=flex_controller,
            config=config.flex,
            on_status=AmplifierWebSocket.broadcast_tune_event,
        )
        target = config.flex.host or "auto-discover"
        logger.info(
            f"Flex control enabled (on-demand): host={target}:{config.flex.port} "
            f"slice={config.flex.slice_rx} "
            f"tune_power={config.flex.tune_power_watts}W — connects when the "
            "Sweep menu opens"
        )

    AmplifierWebSocket.configure(
        serial_handler=serial_handler,
        power_controller=power_controller,
        tune_orchestrator=tune_orchestrator,
        flex_controller=flex_controller,
        heartbeat=config.polling.heartbeat,
    )

    app = make_app()
    app.listen(config.server.port, address=config.server.host)

    logger.info(
        f"Server listening on http://{config.server.host}:{config.server.port}/"
    )
    logger.info(f"Serial port: {config.serial.port} @ {config.serial.baudrate} baud")

    loop = asyncio.get_event_loop()

    # Graceful shutdown
    def shutdown(sig, frame):
        logger.info("Shutting down...")
        # Schedule serial handler stop on the asyncio loop
        try:
            loop.call_soon_threadsafe(loop.create_task, serial_handler.stop())
        except Exception:
            # Fallback: ensure a stop task is scheduled
            loop.call_soon_threadsafe(lambda: loop.create_task(serial_handler.stop()))

        # Stop Tornado IOLoop in a signal-safe way
        tornado.ioloop.IOLoop.current().add_callback_from_signal(
            tornado.ioloop.IOLoop.current().stop
        )

        # Also stop the asyncio loop after pending tasks complete
        loop.call_soon_threadsafe(loop.stop)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Start serial handler + presence-heartbeat as async tasks
    serial_task = loop.create_task(serial_handler.start())
    heartbeat_task = loop.create_task(
        presence_heartbeat_loop(
            serial_handler,
            config.polling.presence_heartbeat,
            config.polling.amp_alive_threshold,
        )
    )
    logger.info(
        f"Presence heartbeat every {config.polling.presence_heartbeat:.1f}s "
        f"(amp_alive_threshold={config.polling.amp_alive_threshold:.1f}s)"
    )

    # No Flex connect at startup — the FlexController opens the SmartSDR
    # session on demand (Sweep-menu open / tune start) and closes it when
    # the cycle is over. See the on-demand lifecycle note above.

    try:
        tornado.ioloop.IOLoop.current().start()
    finally:
        # Ensure background tasks are cancelled if still running
        cleanup_tasks = [serial_task, heartbeat_task]
        for task in cleanup_tasks:
            try:
                if not task.done():
                    task.cancel()
            except Exception:
                pass

        # Close the Flex socket if a tune cycle left it open.
        if flex_controller is not None:
            try:
                loop.run_until_complete(flex_controller.disconnect())
            except Exception:
                logger.exception("Error while closing Flex connection")

        # Make a best-effort to stop serial handler and close resources
        try:
            loop.run_until_complete(serial_handler.stop())
        except Exception:
            logger.exception("Error while stopping serial handler")

        logger.info("Server stopped")


if __name__ == "__main__":
    main()
