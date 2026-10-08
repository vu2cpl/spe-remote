#!/usr/bin/env python3
"""Generate the SPE Remote Control User Guide PDF."""

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm, cm
from reportlab.lib.colors import HexColor
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, HRFlowable, KeepTogether,
)

# Colors
DARK_BG = HexColor("#1a1a2e")
ACCENT = HexColor("#00bcd4")
PANEL = HexColor("#16213e")
TEXT_LIGHT = HexColor("#333333")
HEADER_BG = HexColor("#0f3460")
WHITE = HexColor("#ffffff")
BLACK = HexColor("#000000")
LIGHT_GREY = HexColor("#f0f0f0")
MED_GREY = HexColor("#cccccc")
SUCCESS = HexColor("#4caf50")
DANGER = HexColor("#c62828")

# Styles
style_title = ParagraphStyle(
    "Title", fontName="Helvetica-Bold", fontSize=28,
    textColor=HEADER_BG, alignment=TA_CENTER, spaceAfter=6,
)
style_subtitle = ParagraphStyle(
    "Subtitle", fontName="Helvetica", fontSize=14,
    textColor=ACCENT, alignment=TA_CENTER, spaceAfter=20,
)
style_credits = ParagraphStyle(
    "Credits", fontName="Helvetica", fontSize=10,
    textColor=TEXT_LIGHT, alignment=TA_CENTER, spaceAfter=4,
)
style_h1 = ParagraphStyle(
    "H1", fontName="Helvetica-Bold", fontSize=18,
    textColor=HEADER_BG, spaceBefore=16, spaceAfter=8,
)
style_h2 = ParagraphStyle(
    "H2", fontName="Helvetica-Bold", fontSize=13,
    textColor=ACCENT, spaceBefore=12, spaceAfter=6,
)
style_body = ParagraphStyle(
    "Body", fontName="Helvetica", fontSize=10,
    textColor=TEXT_LIGHT, alignment=TA_JUSTIFY,
    spaceBefore=3, spaceAfter=6, leading=14,
)
style_code = ParagraphStyle(
    "Code", fontName="Courier", fontSize=9,
    textColor=BLACK, backColor=LIGHT_GREY,
    spaceBefore=4, spaceAfter=6, leading=13,
    leftIndent=12, rightIndent=12,
    borderPadding=(6, 6, 6, 6),
)
style_bullet = ParagraphStyle(
    "Bullet", fontName="Helvetica", fontSize=10,
    textColor=TEXT_LIGHT, leftIndent=20, bulletIndent=8,
    spaceBefore=2, spaceAfter=2, leading=14,
)
style_note = ParagraphStyle(
    "Note", fontName="Helvetica-Oblique", fontSize=9,
    textColor=HexColor("#666666"), leftIndent=12,
    spaceBefore=4, spaceAfter=8, leading=12,
)

def hr():
    return HRFlowable(width="100%", thickness=0.5, color=MED_GREY, spaceBefore=8, spaceAfter=8)

def bullet(text):
    return Paragraph(f"\u2022  {text}", style_bullet)

def code(text):
    return Paragraph(text.replace("\n", "<br/>"), style_code)

def styled_table(data, col_widths, font_size=9, courier_first_col=False):
    t = Table(data, colWidths=col_widths)
    style = [
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), font_size),
        ("BACKGROUND", (0, 0), (-1, 0), HEADER_BG),
        ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
        ("TEXTCOLOR", (0, 1), (-1, -1), TEXT_LIGHT),
        ("GRID", (0, 0), (-1, -1), 0.5, MED_GREY),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, LIGHT_GREY]),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]
    if courier_first_col:
        style.append(("FONTNAME", (0, 1), (0, -1), "Courier"))
        style.append(("FONTNAME", (1, 1), (-1, -1), "Helvetica"))
    else:
        style.append(("FONTNAME", (0, 1), (-1, -1), "Helvetica"))
    t.setStyle(TableStyle(style))
    return t


def build_pdf(output_path):
    doc = SimpleDocTemplate(
        output_path, pagesize=A4,
        leftMargin=2*cm, rightMargin=2*cm,
        topMargin=2*cm, bottomMargin=2*cm,
        title="SPE Remote Control User Guide",
        author="VU2CPL",
    )

    story = []

    # ==========================================================
    # COVER
    # ==========================================================
    story.append(Spacer(1, 60))
    story.append(Paragraph("SPE Amplifier<br/>Remote Control", style_title))
    story.append(Spacer(1, 8))
    story.append(Paragraph("User Guide", style_subtitle))
    story.append(Spacer(1, 20))
    story.append(hr())
    story.append(Spacer(1, 10))
    story.append(Paragraph("For SPE Expert 1.3K-FA / 1.5K-FA / 2K-FA HF Amplifiers", style_credits))
    story.append(Spacer(1, 30))
    story.append(Paragraph("Original script by <b>OH2GEK</b>", style_credits))
    story.append(Paragraph("Modernized and extended by <b>VU2CPL</b>", style_credits))
    story.append(Spacer(1, 40))

    info_data = [
        ["Version", "3.0.0 (Flex orchestration + band sweep)"],
        ["Platform", "Raspberry Pi / Linux / macOS"],
        ["Interface", "Web browser + MacExpert native"],
        ["Connection", "USB / RS-232 Serial"],
        ["Protocol", "SPE App Programmer's Guide Rev 1.1"],
    ]
    story.append(styled_table(
        [["Item", "Value"]] + info_data,
        [4*cm, 9*cm], font_size=10,
    ))

    story.append(PageBreak())

    # ==========================================================
    # TOC
    # ==========================================================
    story.append(Paragraph("Table of Contents", style_h1))
    story.append(hr())
    toc_items = [
        "1. Introduction",
        "2. Requirements",
        "3. Installation",
        "4. Configuration",
        "5. Starting the Server",
        "6. Web Interface Guide",
        "7. Power On / Off Control",
        "8. Controls Reference",
        "9. RCU (Remote Control Unit) Mode",
        "10. Architecture",
        "11. SPE Serial Protocol Reference",
        "12. Running as a System Service",
        "13. WebSocket API",
        "14. Troubleshooting",
    ]
    for item in toc_items:
        story.append(Paragraph(item, style_body))
    story.append(PageBreak())

    # ==========================================================
    # 1. INTRODUCTION
    # ==========================================================
    story.append(Paragraph("1. Introduction", style_h1))
    story.append(hr())
    story.append(Paragraph(
        "SPE Remote Control is a modern Python 3 application that allows you to monitor and "
        "control SPE Expert HF amplifiers remotely from any web browser or from the MacExpert "
        "native macOS companion app. It communicates with the amplifier via USB or RS-232 and "
        "serves a real-time web dashboard plus a binary RCU LCD mirror over a single WebSocket.",
        style_body,
    ))
    story.append(Paragraph("Key Features:", style_h2))
    for feat in [
        "<b>Power On/Off</b> - remote power control via DTR line (on) and serial command 0x0A (off).",
        "<b>Full SPE protocol</b> - all commands from the official Programmer's Guide plus undocumented RCU commands.",
        "<b>RCU LCD mirror</b> - live front-panel display streamed as binary frames for pixel-accurate rendering.",
        "<b>Self-contained</b> - single process serves both the WebSocket API and web UI.",
        "<b>Multi-client</b> - multiple browsers and devices simultaneously.",
        "<b>Mixed-client broadcast</b> - text JSON for browsers, binary frames for RCU clients, same socket.",
        "<b>Threaded reader + async writer</b> - survives USB-serial poll glitches that crash serial_asyncio.",
        "<b>Graceful shutdown</b> - SIGINT/SIGTERM handler cancels tasks and closes the port cleanly.",
        "<b>Auto-reconnect</b> - WebSocket and serial both reconnect automatically on failure.",
        "<b>Configurable</b> - YAML configuration file for serial port, baud, polling, heartbeat.",
    ]:
        story.append(bullet(feat))

    story.append(PageBreak())

    # ==========================================================
    # 2. REQUIREMENTS
    # ==========================================================
    story.append(Paragraph("2. Requirements", style_h1))
    story.append(hr())
    story.append(Paragraph("Hardware:", style_h2))
    for req in [
        "Raspberry Pi (any model - Pi 2, 3, 4, 5, or Zero 2 W) or any Linux/macOS computer",
        "SPE Expert amplifier (1.3K-FA, 1.5K-FA, 2K-FA, or compatible model)",
        "USB cable (with FTDI adapter) or RS-232 serial connection to the amplifier",
        "Network connection (Ethernet or Wi-Fi)",
    ]:
        story.append(bullet(req))

    story.append(Paragraph("Software:", style_h2))
    for req in [
        "Python 3.9 or newer (included in Raspberry Pi OS)",
        "python3-venv package (for virtual environment)",
        "Git (for cloning the repository)",
    ]:
        story.append(bullet(req))

    story.append(Paragraph("Python Dependencies (installed automatically):", style_h2))
    story.append(styled_table(
        [
            ["Package", "Version", "Purpose"],
            ["tornado", ">= 6.0", "Web server and WebSocket framework"],
            ["pyserial", ">= 3.5", "Serial port (blocking reads on reader thread)"],
            ["pyyaml", ">= 6.0", "YAML configuration file parsing"],
        ],
        [4*cm, 3*cm, 7*cm],
    ))

    story.append(PageBreak())

    # ==========================================================
    # 3. INSTALLATION
    # ==========================================================
    story.append(Paragraph("3. Installation", style_h1))
    story.append(hr())

    story.append(Paragraph("Step 1: Clone the Repository", style_h2))
    story.append(code(
        "git clone https://github.com/vu2cpl/spe-remote.git<br/>"
        "cd spe-remote"
    ))

    story.append(Paragraph("Step 2: Run Setup", style_h2))
    story.append(code("./setup.sh"))
    story.append(Paragraph(
        "The setup script creates a Python virtual environment and installs all dependencies. "
        "On Raspberry Pi OS, it will also install python3-venv if needed.",
        style_body,
    ))

    story.append(Paragraph("Step 3: Add User to dialout Group", style_h2))
    story.append(code("sudo usermod -aG dialout $USER"))
    story.append(Paragraph("Log out and back in for the change to take effect.", style_note))

    story.append(PageBreak())

    # ==========================================================
    # 4. CONFIGURATION
    # ==========================================================
    story.append(Paragraph("4. Configuration", style_h1))
    story.append(hr())
    story.append(Paragraph(
        "All settings are in <b>config.yaml</b> in the project root.",
        style_body,
    ))
    story.append(code(
        "serial:<br/>"
        "&nbsp;&nbsp;port: /dev/serial/by-id/usb-FTDI_FT232R_USB_UART_...<br/>"
        "&nbsp;&nbsp;baudrate: 115200<br/>"
        "&nbsp;&nbsp;timeout: 1.0<br/>"
        "<br/>"
        "server:<br/>"
        "&nbsp;&nbsp;port: 8888<br/>"
        '&nbsp;&nbsp;host: "0.0.0.0"<br/>'
        "<br/>"
        "polling:<br/>"
        "&nbsp;&nbsp;tx_interval: 0.2<br/>"
        "&nbsp;&nbsp;idle_interval: 1.0<br/>"
        "&nbsp;&nbsp;heartbeat: 15<br/>"
        "<br/>"
        "logging:<br/>"
        "&nbsp;&nbsp;level: INFO"
    ))

    story.append(Paragraph("Configuration Options:", style_h2))
    story.append(styled_table(
        [
            ["Setting", "Default", "Description"],
            ["serial.port", "/dev/ttyUSB0", "Serial port path to amplifier"],
            ["serial.baudrate", "115200", "Serial baud rate"],
            ["server.port", "8888", "HTTP/WebSocket listen port"],
            ["server.host", "0.0.0.0", "Listen address (0.0.0.0 = all ifaces)"],
            ["polling.tx_interval", "0.2", "Poll interval during TX (sec)"],
            ["polling.idle_interval", "1.0", "Poll interval during RX/Standby"],
            ["polling.heartbeat", "15", "Force state broadcast interval"],
            ["updates.check", "true", "Daily GitHub new-release check (banner)"],
            ["logging.level", "INFO", "DEBUG / INFO / WARNING / ERROR"],
        ],
        [4*cm, 2.5*cm, 7.5*cm], font_size=8,
    ))

    story.append(Paragraph("Finding Your Serial Port:", style_h2))
    story.append(code(
        "ls /dev/serial/by-id/<br/>"
        "dmesg | grep ttyUSB"
    ))
    story.append(Paragraph(
        "Tip: Use /dev/serial/by-id/... paths instead of /dev/ttyUSB0. "
        "The by-id paths are stable across reboots.",
        style_note,
    ))

    story.append(PageBreak())

    # ==========================================================
    # 5. STARTING
    # ==========================================================
    story.append(Paragraph("5. Starting the Server", style_h1))
    story.append(hr())
    story.append(Paragraph("Interactive (foreground):", style_body))
    story.append(code("./run.sh"))
    story.append(Paragraph("Detached with log to nohup.out:", style_body))
    story.append(code("nohup ./run.sh &"))
    story.append(Paragraph("Or start directly with the venv Python:", style_body))
    story.append(code("venv/bin/python server.py"))
    story.append(Paragraph(
        "With a custom config — pass the path to your YAML config file as "
        "the first argument. The file must exist; if it doesn't, the server "
        "exits with an error rather than silently falling back to defaults.",
        style_body,
    ))
    story.append(code("venv/bin/python server.py YOUR-CONFIG.yaml"))
    story.append(Paragraph("On successful startup you will see:", style_body))
    story.append(code(
        "[INFO] spe: Server listening on http://0.0.0.0:8888/<br/>"
        "[INFO] spe: Serial port: /dev/ttyUSB0 @ 115200 baud<br/>"
        "[INFO] spe.serial_handler: Connecting...<br/>"
        "[INFO] spe.serial_handler: Serial connected"
    ))
    story.append(Paragraph(
        "Stop with Ctrl+C - the shutdown handler cancels all tasks, "
        "sends RCU OFF to the amp, and closes the port cleanly.",
        style_body,
    ))

    story.append(PageBreak())

    # ==========================================================
    # 6. WEB INTERFACE
    # ==========================================================
    story.append(Paragraph("6. Web Interface Guide", style_h1))
    story.append(hr())
    story.append(Paragraph(
        "The bundled browser dashboard at http://&lt;pi&gt;:8888/ decodes "
        "text JSON state messages and ignores the binary RCU frames. "
        "For the full LCD mirror, use the MacExpert native client.",
        style_body,
    ))

    story.append(Paragraph("Connection Indicator", style_h2))
    story.append(Paragraph(
        "The green/red dot at the top shows WebSocket connection status. "
        "On disconnect, the client auto-reconnects with exponential backoff.",
        style_body,
    ))

    story.append(Paragraph("Power Output Display", style_h2))
    story.append(Paragraph(
        "The large power bar shows current output power in watts (0-1500W). "
        "The gradient color changes from green through cyan and yellow to red.",
        style_body,
    ))

    story.append(Paragraph("Gauges", style_h2))
    story.append(styled_table(
        [
            ["Gauge", "Range", "Warning Zone"],
            ["SWR", "1:1 to 1:3.5", "Above 1:2.0 (orange), above 1:2.8 (red)"],
            ["Drain Current", "0 - 60 A", "Above 42 A (orange), above 51 A (red)"],
            ["PA Temperature", "0 - 80 C", "Above 60 C (orange), above 64 C (red)"],
            ["Voltage", "40 - 60 V", "Below 44 V or above 55 V (warning)"],
        ],
        [3.5*cm, 3.5*cm, 7*cm],
    ))

    story.append(Paragraph("Status Chips", style_h2))
    story.append(Paragraph(
        "Status row shows TX/RX (red pulse during TX, green during RX), "
        "current band, antenna number, input number, and power level.",
        style_body,
    ))

    story.append(Paragraph("Alert Bar", style_h2))
    story.append(Paragraph(
        "Warning and error codes from the amplifier show here in orange "
        "(warning) or red (error). Power action results show green (ok) or "
        "red (error), auto-clearing after 4 seconds.",
        style_body,
    ))

    story.append(PageBreak())

    # ==========================================================
    # 7. POWER CONTROL
    # ==========================================================
    story.append(Paragraph("7. Power On / Off Control", style_h1))
    story.append(hr())
    story.append(Paragraph(
        "The web interface includes Power ON and Power OFF buttons. "
        "These use different mechanisms as defined by the SPE protocol.",
        style_body,
    ))

    story.append(Paragraph("Power ON (DTR hardware toggle)", style_h2))
    story.append(Paragraph(
        "There is no serial data command for power on. Power ON is performed "
        "by toggling the DTR line on the USB-serial adapter. The sequence is "
        "based on the original power_spe_on.py script by OH2GEK:",
        style_body,
    ))
    story.append(code("DTR=1 -> DTR=0 -> RTS=1 -> wait 1s -> DTR=1 -> RTS=0"))
    story.append(Paragraph(
        "After the sequence, the amplifier takes 3 to 4.5 seconds to start. "
        "While DTR is held high, the amp shows 'POWER SWITCH HELD BY REMOTE' "
        "and the front panel switch is overridden.",
        style_note,
    ))

    story.append(Paragraph("Power OFF (serial command 0x0A)", style_h2))
    story.append(Paragraph(
        "Power OFF uses the official SPE serial command <b>SWITCH OFF (0x0A)</b> "
        "from the Application Programmer's Guide - equivalent to pressing "
        "the front-panel OFF button.",
        style_body,
    ))
    story.append(code("Packet: 0x55 0x55 0x55 0x01 0x0A 0x0A"))

    story.append(styled_table(
        [
            ["Action", "Method", "Mechanism"],
            ["Power ON", "DTR hardware toggle", "USB-serial DTR/RTS line sequence"],
            ["Power OFF", "Serial command 0x0A", "SWITCH OFF per SPE protocol"],
        ],
        [3*cm, 4*cm, 7*cm],
    ))

    story.append(Paragraph("Safety:", style_h2))
    story.append(Paragraph(
        "Both Power ON and Power OFF require a browser confirmation dialog. "
        "The buttons show a shimmer animation while processing and display "
        "a success or error result in the alert bar.",
        style_body,
    ))

    story.append(PageBreak())

    # ==========================================================
    # 8. CONTROLS
    # ==========================================================
    story.append(Paragraph("8. Controls Reference", style_h1))
    story.append(hr())
    story.append(Paragraph(
        "All commands below are sent to the amp via WebSocket text messages. "
        "Clients just send the bare command name (e.g. 'oper'); the server "
        "wraps it in the SPE packet format.",
        style_body,
    ))
    story.append(styled_table(
        [
            ["Command", "Hex", "Description"],
            ["power_on", "DTR", "Power ON via DTR toggle. Confirmation required."],
            ["power_off", "0x0A", "SWITCH OFF - power down. Confirmation required."],
            ["oper", "0x0D", "Toggle Operate / Standby."],
            ["antenna", "0x04", "Cycle TX antenna (ANT 1, ANT 2, ...)."],
            ["tune", "0x09", "Start ATU tuning cycle."],
            ["input", "0x01", "Toggle input (IN 1 / IN 2)."],
            ["power_level", "0x0B", "Cycle Low / Mid / High power."],
            ["band_up", "0x03", "Step up one band."],
            ["band_dn", "0x02", "Step down one band."],
            ["l_plus / l_minus", "0x06 / 0x05", "ATU inductance + / -"],
            ["c_plus / c_minus", "0x08 / 0x07", "ATU capacitance + / -"],
            ["left / right / set", "0x0F / 0x10 / 0x11", "Menu navigation"],
            ["display", "0x0C", "Toggle display."],
            ["cat", "0x0E", "CAT mode."],
            ["rcu_on / rcu_off", "0x80 / 0x81", "Enable / disable LCD mirror stream."],
            ["backlight_on / off", "0x82 / 0x83", "Backlight control."],
        ],
        [3.5*cm, 2.5*cm, 8*cm], font_size=8, courier_first_col=True,
    ))
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        "Note: All connected clients can send commands. In a multi-client "
        "setup, coordinate to avoid conflicts.",
        style_note,
    ))

    story.append(PageBreak())

    # ==========================================================
    # 9. RCU MODE  (NEW)
    # ==========================================================
    story.append(Paragraph("9. RCU (Remote Control Unit) Mode", style_h1))
    story.append(hr())
    story.append(Paragraph(
        "RCU is a streaming mode that mirrors the amplifier front-panel LCD "
        "over the serial link. When enabled, the amp emits binary frames "
        "(marker 0x6A) every time the display changes, alongside regular "
        "CSV status polling.",
        style_body,
    ))

    story.append(Paragraph("How the Server Uses RCU", style_h2))
    for step in [
        "On serial connect, the handler sends CMD_REQUEST (status) followed by CMD_RCU_ON.",
        "A background task cycles RCU_OFF -> RCU_ON every 500 ms to keep the stream alive (the amp sometimes stops emitting after a long quiet period).",
        "A quiet-flush task force-terminates any half-received RCU frame after 300 ms of silence - so static screens still emit their final frame.",
        "Incoming RCU payloads are passed to the on_rcu_frame callback, which broadcasts them as binary WebSocket messages.",
    ]:
        story.append(bullet(step))

    story.append(Paragraph("Why Two Frame Types?", style_h2))
    story.append(Paragraph(
        "<b>CSV status</b> - machine-readable data (power, SWR, temps, warnings). "
        "Parsed into JSON, sent as WebSocket text. This is what the browser dashboard reads.",
        style_body,
    ))
    story.append(Paragraph(
        "<b>RCU frames</b> - pixel-level view of the LCD. Lets a native client "
        "render an exact replica of the front panel, including menu screens "
        "and settings that aren't in the CSV.",
        style_body,
    ))

    story.append(Paragraph("Client Matrix", style_h2))
    story.append(styled_table(
        [
            ["Client", "Platform", "Uses CSV", "Uses RCU"],
            ["Web dashboard", "Browser", "Yes", "No (drops binary)"],
            ["MacExpert", "macOS", "Yes", "Yes"],
        ],
        [4*cm, 3*cm, 3*cm, 4*cm],
    ))

    story.append(Paragraph("Command Contract with MacExpert", style_h2))
    story.append(Paragraph(
        "The COMMANDS dict keys in spe/protocol.py are the canonical "
        "WebSocket command names. MacExpert's SPEProtocol.swift has a "
        "matching wsCommandName enum so both clients drive the amp "
        "identically. When adding a new command, update both sides.",
        style_body,
    ))

    story.append(PageBreak())

    # ==========================================================
    # 10. ARCHITECTURE  (NEW)
    # ==========================================================
    story.append(Paragraph("10. Architecture", style_h1))
    story.append(hr())

    story.append(Paragraph("Threading Model (serial_handler.py)", style_h2))
    story.append(Paragraph(
        "The serial handler uses a hybrid thread + asyncio model instead "
        "of pyserial-asyncio:",
        style_body,
    ))
    story.append(code(
        "Asyncio event loop (main thread)<br/>"
        "&nbsp;&nbsp;- _poll_loop: periodic status requests<br/>"
        "&nbsp;&nbsp;- _command_loop: drains queue, writes to port<br/>"
        "&nbsp;&nbsp;- _rcu_tick_loop: keeps RCU stream alive<br/>"
        "&nbsp;&nbsp;- _quiet_flush_loop: flushes stalled RCU frames<br/>"
        "&nbsp;&nbsp;- _connection_watchdog<br/>"
        "&nbsp;&nbsp;- Frame parser: drains receive buffer<br/>"
        "<br/>"
        "Daemon thread<br/>"
        "&nbsp;&nbsp;- Blocking serial.Serial.read() loop<br/>"
        "&nbsp;&nbsp;- Pushes chunks via call_soon_threadsafe()<br/>"
        "<br/>"
        "Synchronization<br/>"
        "&nbsp;&nbsp;- threading.Lock on all writes (_safe_write)<br/>"
        "&nbsp;&nbsp;- threading.Event to signal reader shutdown"
    ))

    story.append(Paragraph("Why not serial_asyncio?", style_h2))
    story.append(Paragraph(
        "Its internal _read_ready callback raises "
        "SerialException(\"readiness to read but returned no data\") on "
        "Linux USB-serial adapters under moderate traffic. That bounces "
        "the port and breaks the RCU stream. The blocking serial.read() "
        "path used here never hits that bug, so the port stays up even "
        "under full RCU load.",
        style_body,
    ))

    story.append(Paragraph("Key Constants", style_h2))
    story.append(styled_table(
        [
            ["Constant", "Value", "Purpose"],
            ["_RCU_TICK_INTERVAL", "0.5 s", "RCU OFF->ON cycle cadence"],
            ["_RCU_OFF_ON_GAP", "0.05 s", "Gap between RCU OFF and ON"],
            ["_RCU_QUIET_FLUSH", "0.3 s", "Force-flush stuck RCU frames"],
            ["_READ_TIMEOUT", "0.1 s", "Blocking serial read timeout"],
            ["_MAX_BUFFER", "4096 bytes", "Receive buffer cap"],
        ],
        [4.5*cm, 2.5*cm, 7*cm], font_size=8, courier_first_col=True,
    ))

    story.append(Paragraph("Lifecycle", style_h2))
    for step in [
        "server.py creates SerialHandler, PowerController, and the Tornado app.",
        "Installs SIGINT/SIGTERM handlers that schedule serial_handler.stop() on the asyncio loop, then stop both Tornado and asyncio loops.",
        "serial_handler.start() loops: open port -> spawn reader thread -> run the five asyncio tasks via asyncio.wait(FIRST_COMPLETED) -> tear down on any task exit -> reconnect after 3 s.",
        "On stop(): sets _stop_reader event, sends RCU_OFF to the amp, closes the port, drops queued commands. The reader thread exits once its port handle is closed.",
    ]:
        story.append(bullet(step))

    story.append(PageBreak())

    # ==========================================================
    # 11. PROTOCOL REFERENCE
    # ==========================================================
    story.append(Paragraph("11. SPE Serial Protocol Reference", style_h1))
    story.append(hr())
    story.append(Paragraph(
        "Based on the <b>SPE Application Programmer's Guide Rev 1.1</b> for "
        "Expert 1.3K-FA / 1.5K-FA / 2K-FA. Communication is asynchronous, "
        "8N1, up to 115200 baud (auto-adapts).",
        style_body,
    ))

    story.append(Paragraph("Packet Format", style_h2))
    story.append(code(
        "0x55 0x55 0x55 [CNT] [DATA...] [CHK]       (host -> amp)<br/>"
        "0xAA 0xAA 0xAA [CNT/TYPE] [DATA...] ...    (amp -> host)<br/>"
        "<br/>"
        "For single-byte commands: CNT=0x01, CHK=DATA"
    ))

    story.append(Paragraph("Complete Command Set", style_h2))
    story.append(styled_table(
        [
            ["Hex", "Command", "Description"],
            ["0x01", "INPUT", "Toggle input port"],
            ["0x02", "BAND -", "Band down"],
            ["0x03", "BAND +", "Band up"],
            ["0x04", "ANTENNA", "Cycle TX antenna"],
            ["0x05", "L-", "ATU inductance minus"],
            ["0x06", "L+", "ATU inductance plus"],
            ["0x07", "C-", "ATU capacitance minus"],
            ["0x08", "C+", "ATU capacitance plus"],
            ["0x09", "TUNE", "Start ATU tuning"],
            ["0x0A", "SWITCH OFF", "Power OFF the amplifier"],
            ["0x0B", "POWER", "Toggle power level (L/M/H)"],
            ["0x0C", "DISPLAY", "Display toggle"],
            ["0x0D", "OPERATE", "Toggle Operate/Standby"],
            ["0x0E", "CAT", "CAT mode"],
            ["0x0F", "LEFT ARROW", "Menu navigation left"],
            ["0x10", "RIGHT ARROW", "Menu navigation right"],
            ["0x11", "SET", "Menu enter / set"],
            ["0x80", "RCU ON", "Enable LCD mirror stream (undocumented)"],
            ["0x81", "RCU OFF", "Disable LCD mirror stream (undocumented)"],
            ["0x82", "BACKLIGHT ON", "Turn display backlight on"],
            ["0x83", "BACKLIGHT OFF", "Turn display backlight off"],
            ["0x90", "STATUS", "Request status string"],
        ],
        [2*cm, 3.5*cm, 8.5*cm], font_size=8, courier_first_col=True,
    ))

    story.append(Paragraph("Response Frame Types", style_h2))
    story.append(styled_table(
        [
            ["Marker", "Type", "Length", "Description"],
            ["0x43", "CSV status", "67 + CRC + CRLF", "ASCII comma-separated status"],
            ["0x6A", "RCU frame", "Variable", "Binary LCD display payload"],
        ],
        [2*cm, 3*cm, 3.5*cm, 5.5*cm], font_size=8,
    ))

    story.append(PageBreak())

    story.append(Paragraph("Status String Fields", style_h2))
    story.append(styled_table(
        [
            ["Field", "Len", "Contents"],
            ["ID", "3", "20K (2K-FA) or 13K (1.3K-FA)"],
            ["Stby/Oper", "1", "S or O"],
            ["RX/TX", "1", "R or T"],
            ["Memory", "1", "A, B, or x"],
            ["Input", "1", "1 or 2"],
            ["Band", "2", "00 (160m) to 11 (4m)"],
            ["TX Ant+ATU", "2", "0-6, suffix t/b/a"],
            ["RX Ant", "2", "Antenna number or 0r"],
            ["Power Level", "1", "L, M, or H"],
            ["Output Power", "4", "Watts (0000 on RX)"],
            ["SWR ATU", "5", "VSWR before ATU"],
            ["SWR ANT", "5", "VSWR at antenna"],
            ["V PA", "4", "Supply voltage"],
            ["I PA", "4", "Drain current"],
            ["Temp Upper", "3", "Heatsink temp"],
            ["Temp Lower", "3", "Lower heatsink (2K-FA)"],
            ["Temp Combiner", "3", "Combiner temp (2K-FA)"],
            ["Warnings", "1", "Code, N = none"],
            ["Alarms", "1", "Code, N = none"],
        ],
        [3.5*cm, 1.2*cm, 8.8*cm], font_size=8,
    ))

    story.append(Paragraph("Warning Codes:", style_h2))
    story.append(Paragraph(
        "<b>M</b>=Alarm, <b>A</b>=No antenna, <b>S</b>=SWR, <b>B</b>=No band, "
        "<b>P</b>=Power limit, <b>O</b>=Overheat, <b>Y</b>=ATU N/A, "
        "<b>W</b>=Tune no power, <b>K</b>=ATU bypass, <b>R</b>=Remote hold, "
        "<b>T</b>=Combiner heat, <b>C</b>=Combiner fault, <b>N</b>=None",
        style_body,
    ))
    story.append(Paragraph("Alarm Codes:", style_h2))
    story.append(Paragraph(
        "<b>S</b>=SWR limit, <b>A</b>=Amp protection, <b>D</b>=Overdrive, "
        "<b>H</b>=Excess heat, <b>C</b>=Combiner fault, <b>N</b>=None",
        style_body,
    ))

    story.append(PageBreak())

    # ==========================================================
    # 12. SYSTEMD
    # ==========================================================
    story.append(Paragraph("12. Running as a System Service", style_h1))
    story.append(hr())
    story.append(Paragraph(
        "To start the server automatically on boot:",
        style_body,
    ))
    story.append(Paragraph("Step 1: Create the Service File", style_h2))
    story.append(code("sudo nano /etc/systemd/system/spe-remote.service"))
    story.append(Paragraph("Paste:", style_body))
    story.append(code(
        "[Unit]<br/>"
        "Description=SPE Amplifier Remote Control<br/>"
        "After=network.target<br/>"
        "<br/>"
        "[Service]<br/>"
        "Type=simple<br/>"
        "User=pi<br/>"
        "WorkingDirectory=/home/pi/spe-remote<br/>"
        "ExecStart=/home/pi/spe-remote/venv/bin/python server.py<br/>"
        "Restart=always<br/>"
        "RestartSec=5<br/>"
        "<br/>"
        "[Install]<br/>"
        "WantedBy=multi-user.target"
    ))
    story.append(Paragraph(
        "Adjust User and WorkingDirectory to your actual install path.",
        style_note,
    ))

    story.append(Paragraph("Step 2: Enable and Start", style_h2))
    story.append(code(
        "sudo systemctl daemon-reload<br/>"
        "sudo systemctl enable spe-remote<br/>"
        "sudo systemctl start spe-remote"
    ))

    story.append(Paragraph("Step 3: Check Status", style_h2))
    story.append(code(
        "sudo systemctl status spe-remote<br/>"
        "journalctl -u spe-remote -f"
    ))

    story.append(PageBreak())

    # ==========================================================
    # 13. WEBSOCKET API
    # ==========================================================
    story.append(Paragraph("13. WebSocket API", style_h1))
    story.append(hr())
    story.append(Paragraph(
        "Connect to <b>ws://&lt;host&gt;:8888/ws</b>. The socket carries three "
        "kinds of server-to-client messages: JSON state updates (text), "
        "JSON power-action results (text), and raw RCU LCD frames (binary). "
        "Clients that don't care about RCU should ignore binary messages.",
        style_body,
    ))

    story.append(Paragraph("Server -> Client: Amplifier State (text)", style_h2))
    story.append(code(
        '{<br/>'
        '&nbsp;&nbsp;"op_status": "Oper",<br/>'
        '&nbsp;&nbsp;"tx_status": "TX",<br/>'
        '&nbsp;&nbsp;"input": "1",<br/>'
        '&nbsp;&nbsp;"band": "80m",<br/>'
        '&nbsp;&nbsp;"tx_antenna": "1",<br/>'
        '&nbsp;&nbsp;"p_level": "10",<br/>'
        '&nbsp;&nbsp;"p_out": "1353",<br/>'
        '&nbsp;&nbsp;"swr": "1.54",<br/>'
        '&nbsp;&nbsp;"aswr": "1.12",<br/>'
        '&nbsp;&nbsp;"voltage": "54.6",<br/>'
        '&nbsp;&nbsp;"drain": "27.3",<br/>'
        '&nbsp;&nbsp;"pa_temp": "26",<br/>'
        '&nbsp;&nbsp;"warnings": "",<br/>'
        '&nbsp;&nbsp;"error": ""<br/>'
        '}'
    ))

    story.append(Paragraph("Server -> Client: Power Action Result (text)", style_h2))
    story.append(code(
        '{<br/>'
        '&nbsp;&nbsp;"power_result": "power_on",<br/>'
        '&nbsp;&nbsp;"status": "ok"<br/>'
        '}'
    ))

    story.append(Paragraph("Server -> Client: RCU Frame (binary)", style_h2))
    story.append(Paragraph(
        "Raw bytes after the AA AA AA 6A sync+marker. Decoding is client-"
        "specific; see MacExpert's RCUFrameDecoder.swift for reference.",
        style_body,
    ))

    story.append(Paragraph("Client -> Server: Commands (text)", style_h2))
    story.append(Paragraph(
        "Bare command name, e.g. 'oper'. Server routes power_on to "
        "PowerController.power_on() (DTR toggle), power_off to "
        "PowerController.power_off() (0x0A), everything else to "
        "SerialHandler.send_command().",
        style_body,
    ))

    story.append(Paragraph("JavaScript Client Example", style_h2))
    story.append(code(
        'const ws = new WebSocket("ws://&lt;pi&gt;:8888/ws");<br/>'
        "<br/>"
        "ws.onmessage = (evt) =&gt; {<br/>"
        '&nbsp;&nbsp;if (typeof evt.data === "string") {<br/>'
        "&nbsp;&nbsp;&nbsp;&nbsp;const msg = JSON.parse(evt.data);<br/>"
        "&nbsp;&nbsp;&nbsp;&nbsp;if (msg.power_result) { /* power result */ }<br/>"
        "&nbsp;&nbsp;&nbsp;&nbsp;else                  { /* state update */ }<br/>"
        "&nbsp;&nbsp;} else {<br/>"
        "&nbsp;&nbsp;&nbsp;&nbsp;// Binary = RCU LCD frame<br/>"
        "&nbsp;&nbsp;&nbsp;&nbsp;evt.data.arrayBuffer().then(buf =&gt; renderRCU(new Uint8Array(buf)));<br/>"
        "&nbsp;&nbsp;}<br/>"
        "};<br/>"
        "<br/>"
        'ws.send("oper");<br/>'
        'ws.send("band_up");<br/>'
        'ws.send("power_off");'
    ))

    story.append(PageBreak())

    # ==========================================================
    # 14. TROUBLESHOOTING
    # ==========================================================
    story.append(Paragraph("14. Troubleshooting", style_h1))
    story.append(hr())

    issues = [
        ("Serial error: No such file or directory",
         "The serial port path in config.yaml does not exist. Check the cable "
         "is plugged in and verify with: ls /dev/serial/by-id/"),
        ("Permission denied on serial port",
         "Add your user to the dialout group: sudo usermod -aG dialout $USER, "
         "then log out and back in."),
        ("Web page not loading",
         "Check the server is running and the port isn't blocked by a firewall. "
         "Try: sudo ufw allow 8888/tcp"),
        ("Gauges not updating",
         "Open browser dev tools (F12) and check for WebSocket errors. "
         "Verify the server IP and port are correct."),
        ("Multiple /dev/ttyUSB devices",
         "Use /dev/serial/by-id/ which is unique per adapter."),
        ("Power ON not working",
         "Verify your FTDI USB-serial adapter supports DTR line control. "
         "Check dmesg for adapter recognition. DTR toggle requires hardware support."),
        ("'POWER SWITCH HELD BY REMOTE' warning",
         "Normal when DTR is held high after a remote power on. The front panel "
         "switch is overridden while the remote has control."),
        ("'Suppressed spurious USB-serial poll glitch' in logs",
         "Harmless. The Linux USB-serial stack sometimes lies about poll readiness - "
         "the reader thread handles it without dropping the port."),
        ("RCU frames never arrive",
         "Check the companion client actually reads binary WebSocket messages - "
         "the browser dashboard drops them silently by design."),
        ("Server doesn't exit on Ctrl+C",
         "Shouldn't happen with the new shutdown handler. If it does, check for a "
         "hung serial read and report the issue."),
        ("MacExpert can't send commands",
         "Verify the wsCommandName enum in Swift matches the COMMANDS keys in "
         "spe/protocol.py. Both sides must be kept in sync when adding commands."),
        ("Server crashes on startup",
         "Check the log for errors. Common causes: wrong Python version (need 3.9+), "
         "missing dependencies (re-run ./setup.sh), or serial port in use by another program."),
        ("High CPU usage",
         "Increase polling.idle_interval in config.yaml to reduce poll frequency when idle. "
         "Default 1.0 s is fine for most setups."),
    ]
    for problem, solution in issues:
        story.append(KeepTogether([
            Paragraph(f"<b>Problem:</b> {problem}", style_body),
            Paragraph(f"<b>Solution:</b> {solution}", style_body),
            Spacer(1, 8),
        ]))

    story.append(hr())
    story.append(Spacer(1, 12))
    story.append(Paragraph(
        "For additional help, open an issue on the GitHub repository:<br/>"
        "https://github.com/vu2cpl/spe-remote",
        style_body,
    ))
    story.append(Spacer(1, 30))
    story.append(Paragraph(
        "73 de OH2GEK &amp; VU2CPL",
        ParagraphStyle("Footer", fontName="Helvetica-Bold", fontSize=12,
                       textColor=ACCENT, alignment=TA_CENTER),
    ))

    doc.build(story)
    print(f"PDF created: {output_path}")


if __name__ == "__main__":
    build_pdf("docs/SPE_Remote_Control_User_Guide.pdf")
