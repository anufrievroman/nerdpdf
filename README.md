# nerdpdf

Minimal keyboard-driven PDF viewer (PyMuPDF + Qt/PySide6).

## Install / run

    python3 -m venv .venv
    .venv/bin/pip install -e .
    .venv/bin/nerdpdf file.pdf        # or: .venv/bin/python -m nerdpdf file.pdf

## Keys

| Key | Action |
| --- | --- |
| `j` / `k` (↓ / ↑) | scroll down / up |
| `h` / `l` (← / →) | scroll left / right |
| `J` / `K`, `n` / `p` | next / previous page |
| `Space` / `Shift+Space`, PgDn / PgUp | scroll one screen |
| `gg` / `G` | first / last page; `12G` or `12gg` goes to page 12 |
| `a` / `s` | fit width / fit page |
| `=` / `-` | zoom in / out |
| Shift+wheel, Cmd/Ctrl+wheel, pinch | zoom at the cursor |
| `/` | search: Enter = next, Shift+Enter = previous, Esc = close |
| `o` | open file (drag & drop works too) |
| `q` | quit |

Counts work like vim: `5j`, `3J`.

Zoom / fit mode, window geometry and the last position in each file are remembered
(QSettings). Background follows the system light/dark theme.
