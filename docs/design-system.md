# Design system — palette adapted from dirigobtc.org

Extracted 2026-06-15 from `dirigobtc.org/styles.css`. Identity: **warm cream + deep navy + Bitcoin orange**, slate-gray secondary text, warm-tan borders. Headings in a serif/Trebuchet display face, body in a clean sans.

## Core palette

| Token | Hex | Role |
|---|---|---|
| `--page` | `#fff8eb` | Page background (warm cream) |
| `--surface` | `#fffaf2` | Cards / panels |
| `--surface-blue` | `#edf4fb` | Soft blue panel (alt surface) |
| `--ink` | `#14202f` | Body text |
| `--heading` | `#10233b` | Headings (deep navy) |
| `--heading-alt` | `#072f4c` | Deep teal-navy (alt heading) |
| `--muted` | `#566a77` | Secondary / muted text |
| `--line` | `#ded3c1` | Borders / dividers (warm tan) |
| `--accent` | `#f7931a` | **Bitcoin orange** — primary accent |
| `--accent-deep` | `#df7419` | Burnt-orange (hover/active) |
| `--dark-bg` | `#10233b` | Inverted sections (feature/footer) navy |
| `--dark-ink` | `#fff7e9` | Text on dark sections |
| `--dark-copy` | `#dce7ef` | Muted text on dark sections |
| `--tag-bg` | `#f8dfbd` | Badge/tag background (warm) |
| `--tag-ink` | `#5a320c` | Badge/tag text (brown) |

## Buttons / shape / type

- Primary button: bg `#10233b`, text `#fff7e9`; secondary button: bg `#fffaf0`, text `#10233b`, border `#ded3c1`.
- Radius: `--radius: 14px` (cards), `8px` (buttons). Shadows: soft navy, e.g. `0 22px 60px rgba(16,35,59,0.12)`.
- Display font: `Georgia, "Trebuchet MS", serif` feel. Body font: `Inter, "Segoe UI", system-ui, sans-serif`.

## Additions needed for a tax app (not in the source palette — harmonized)

- `--gain: #1d7a5f` (capital gain / positive) · `--loss: #b3402e` (loss / negative) · `--warn: #b8791b`.
- Optional later: a dark mode variant (invert `--page`/`--ink` toward the existing `--dark-bg` navy family).

## Ready-to-use CSS

```css
:root {
  --page:#fff8eb; --surface:#fffaf2; --surface-blue:#edf4fb;
  --ink:#14202f; --heading:#10233b; --heading-alt:#072f4c; --muted:#566a77;
  --line:#ded3c1; --accent:#f7931a; --accent-deep:#df7419;
  --dark-bg:#10233b; --dark-ink:#fff7e9; --dark-copy:#dce7ef;
  --tag-bg:#f8dfbd; --tag-ink:#5a320c;
  --gain:#1d7a5f; --loss:#b3402e; --warn:#b8791b;
  --radius:14px; --radius-btn:8px;
  --font-display:Georgia,"Trebuchet MS",serif;
  --font-body:Inter,"Segoe UI",system-ui,sans-serif;
}
```
