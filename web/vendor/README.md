# Vendored libraries

Kept in the repository so the app needs no CDN, no build step and works offline.

| File | Library | Version | Licence | Source |
|---|---|---|---|---|
| `anime.umd.min.js` | [anime.js](https://animejs.com) | 4.5.0 | MIT (`anime.LICENSE.md`) | npm `animejs@4.5.0`, `dist/bundles/anime.umd.min.js` |

SHA-256 of `anime.umd.min.js`: `8d5b3a58a1f64023a04a4cedeef135a2263b18da04b35177ac13438a0bad033b`

Used only for the Dashboard's arrival animations (`animateDashboard` in `web/app.js`): none
with the system's "reduce motion" setting, and the page works unchanged if the file is missing.
