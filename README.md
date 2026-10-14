# PCB Copilot Core

Describe a circuit board in plain English and get a KiCad 10 project back: real in-stock JLCPCB/LCSC parts,
a schematic, a placed and autorouted 2-layer board checked with KiCad's own ERC and DRC, and the fab files.

This is the core of [PCB Copilot](https://github.com/m8rix-one) made public: the Claude + KiCad integration,
kept small enough to read in an afternoon. One principle runs through it:

> **The model decides. Tools act. KiCad checks.**
> Claude chooses the parts and the connections, but it never writes a CAD file. It works through a dozen typed
> tools over a circuit IR; every edit is validated against the real symbol, so an invented pin is an error the
> model sees, not a wire on your board. Deterministic code then draws the schematic and the board, and KiCad's
> ERC and DRC judge the result.

```
"USB-C powered ESP32-C3 sensor board with a BME280 and a status LED"
      │
      ▼  design agent: Claude (Claude Code CLI over MCP, or the Anthropic API) + 14 tools
  search the JLCPCB/LCSC catalog ─ read datasheets (page-cited) ─ add parts ─ connect pins ─ build
      │      every part → KiCad symbol + footprint + 3D model (LCSC/EasyEDA or KiCad's own libraries)
      ▼
  circuit IR (design.json): parts, nets, no-connects. Validated edits only.
      ▼
  KiCad schematic (global labels) ─ KiCad ERC
      ▼
  placement ─ KiCad board (pcbnew) ─ Freerouting ─ GND pours + stitching vias ─ KiCad DRC
      ▼
  Gerbers, Excellon drill, JLCPCB BOM + CPL, STEP
```

## Install

Requirements: Python 3.11+, [KiCad 10](https://www.kicad.org/download/), Java 21+ (for the Freerouting
autorouter; `brew install openjdk`), and either [Claude Code](https://claude.com/claude-code) (`claude` on your
PATH, signed in) or an `ANTHROPIC_API_KEY`.

```bash
git clone https://github.com/m8rix-one/pcb-copilot-core.git
cd pcb-copilot-core
python3 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Use

```bash
# a board from a brief: the agent designs it, then the pipeline compiles it to KiCad and runs ERC/DRC
python -m pcbcopilot design "USB-C powered ESP32-C3 sensor board with a BME280 and a status LED"

# change it
python -m pcbcopilot chat usb-c-powered-esp32-c3-sensor-board "add a reset button and a 3-pin UART header"

# rebuild without AI (same design.json, same board every time), and make the fab files
python -m pcbcopilot build usb-c-powered-esp32-c3-sensor-board
python -m pcbcopilot release usb-c-powered-esp32-c3-sensor-board
```

Projects live in `~/.pcb-copilot-core/projects/<id>/`: `design.json` (the circuit IR), `kicad/` (a normal KiCad
project you can open and edit), `build.json` (the last ERC/DRC results) and `release/`.

### The tools as an MCP server

The same tools are an MCP server, so Claude Code, Claude Desktop or Cursor can design a board in a chat:

```json
{"mcpServers": {"pcb": {"command": "/path/to/.venv/bin/python",
                        "args": ["-m", "pcbcopilot", "mcp", "<project id>"],
                        "cwd": "/path/to/pcb-copilot-core"}}}
```

Create the project first (`python -m pcbcopilot design` with any brief, or by hand: a folder with `design.json`
from `pcbcopilot.ir.new()`).

## How it works

| File | What it does |
|---|---|
| `pcbcopilot/agent/runner.py` | The tool-use loop: Claude Code CLI with the tools over MCP, or the Messages API. The system prompt is the whole workflow. |
| `pcbcopilot/agent/tools.py` | The 14 tools: `search_parts`, `get_part`, `read_datasheet`, `add_part`, `add_passive`, `connect`, `disconnect`, `mark_no_connect`, `remove_part`, `replace_part`, `set_value`, `set_board`, `set_info`, `get_design`, `build_board`. |
| `pcbcopilot/ir.py` | The circuit IR and `Design`, the only thing the model edits. Pin names are resolved against the real symbol; what does not exist is refused. |
| `pcbcopilot/catalog.py` | JLCPCB/LCSC catalog search, exact MPN lookup, best in-stock resistor/capacitor/LED. |
| `pcbcopilot/library.py` | Any LCSC number → KiCad symbol, footprint and 3D model (easyeda2kicad), with electrical pin types for ERC. Passives use KiCad's own IPC-7351 footprints. |
| `pcbcopilot/datasheet.py` | Fetches the manufacturer PDF, extracts text per page, answers a question with page citations. |
| `pcbcopilot/schematic.py` | IR → `.kicad_sch`: symbols on a grid, a global label on every connected pin, no-connect flags on open ones. |
| `pcbcopilot/placer.py` | Connectors on the edge you asked for, everything else packed in rows. Move parts in KiCad afterwards. |
| `pcbcopilot/board_script.py` | Runs under KiCad's bundled Python: footprint geometry, the board (outline, nets, footprints, Specctra export), routed-track import, GND pours, stitching vias. |
| `pcbcopilot/build.py` | The deterministic pipeline, KiCad ERC/DRC via `kicad-cli`, Freerouting, release files. |

What is deliberately **not** here (it is in the full PCB Copilot application): the engineering rule checks on the
IR, the library check of footprints against datasheets, fab profiles and IPC-2221 track sizing, hierarchical
schematics and the readability placer, circuit templates, engineering change orders, the bring-up plan,
simulation, the datasheet-intelligence corpus, the web UI, and Altium import/export.

## Tests

```bash
python -m unittest discover -s tests          # offline: IR, schematic writer, placer, s-expressions
```

## License

MIT, see [LICENSE](LICENSE). KiCad, Freerouting and easyeda2kicad are separate projects under their own licenses.
Part data comes from JLCPCB/LCSC through the public jlcsearch mirror; datasheets are the manufacturers'.
