# Brand guideline

CryptoLedger's look (colours, font, logo, favicon) is kept apart from the app's code, so that
anyone who downloads it can re-brand their own copy without touching the logic.

![CryptoLedger brand guideline](brand/brand-guideline.webp)

## The CryptoLedger brand

**Palette**

| Name | Hex | Used for | Token in `web/brand.css` |
|---|---|---|---|
| Night | `#0B0B12` | page background (dark-first) | `--night`, `--bg` |
| Purple 900 | `#5B00FF` | primary actions (buttons, start of the gradient) | `--primary` |
| Purple 600 | `#8A3FFC` | data, active states, focus, end of the gradient | `--primary-2` |
| Purple 200 | `#DCCBFF` | soft accents, charts | `--primary-light` |
| Gold | `#D4AF37` | value, exports, warnings, key highlights | `--highlight` |
| White | `#FFFFFF` | text on Night, text on primary buttons | `--text`, `--accent-text` |

**Gradients**: Purple 900 → Purple 600 for primary buttons and bars (`--gradient`); Night → Gold
for premium highlights.

**Typography**: Inter, weights 400 · 500 · 600 · 700. Bundled in `web/fonts/` (variable font,
SIL Open Font License, see `web/fonts/OFL.txt`), so the app makes no request to font services.

**Logo**: three stacked rounded squares with a "C" (`web/logo.svg`); wordmark "Crypto" in white
and "Ledger" in Purple 600. **App icon / favicon**: the mark on a Night tile (`web/favicon.svg`,
`favicon-32.png`, `apple-touch-icon.png` 180 px, `icon-512.png`; `web/manifest.json` for "install
app").

**Style**: dark-first, high contrast; purple for primary actions and data; gold for value and
key highlights; clean, minimal UI; rounded corners 8–16 px (`--radius-sm` 8 px, `--radius`
14 px, dialogs 16 px); 8 px spacing system; outline icons, 2 px stroke. A light theme is
available from the sidebar ("Light theme"); the choice is remembered in the browser.

## Re-brand your own copy

Everything below lives in `web/`; no Python or JavaScript change is needed.

1. **Colours** — edit the variables at the top of `web/brand.css`:
   - the five palette colours (`--night`, `--primary`, `--primary-2`, `--primary-light`,
     `--highlight`);
   - the semantic tokens below them (`--bg`, `--surface`, `--surface-2`, `--border`, `--text`,
     `--muted`, `--accent`, `--pos`/`--neg`, `--warn`/`--err`, chart colours `--c1`…`--c8`);
   - the `:root[data-theme="light"]` block for the light theme.
   Keep text contrast at least 4.5:1 against `--bg` and `--surface` (check with any contrast
   checker).
2. **Font** — put your `.woff2` files in `web/fonts/`, change the two `@font-face` blocks and
   `--font` in `web/brand.css`. Include the font's licence file. To use the operating-system
   font instead, set `--font: ui-sans-serif, system-ui, sans-serif;` and delete the
   `@font-face` blocks.
3. **Logo** — replace `web/logo.svg` (square, shown at 34 px in the sidebar). The wordmark text
   is in `web/index.html` (`<span class="brand-name">`): the part in `<span class="ledger">` takes
   the `--primary-2` colour.
4. **Favicon and app icons** — replace `web/favicon.svg`, `web/favicon-32.png` (32×32),
   `web/apple-touch-icon.png` (180×180) and `web/icon-512.png` (512×512). Update the name and
   colours in `web/manifest.json` and `<meta name="theme-color">` in `web/index.html`.
5. **Guideline** — replace `docs/brand/brand-guideline.webp` and the tables above, so the next
   person knows the rules.

Reload the page (Ctrl/Cmd + Shift + R to bypass the cache). Run the demo to see every page with
sample data:

```bash
python3 -m cryptoledger demo --port 8799
```

## Rules for code changes

- Never hard-code a colour, font or radius in `web/style.css` or `web/app.js`: use the variables
  of `web/brand.css` (`var(--primary)`, `var(--highlight)`, `var(--radius)`…). Charts read
  `--c1`…`--c8` at run time.
- Primary actions use `.btn.primary` (gradient); exports and downloads use `.btn.highlight`
  (gold outline); dangerous actions use `.btn.danger`.
- Status colours keep their meaning: `--pos` gains, `--neg` losses, `--warn` (gold) to review,
  `--err` blocking.
