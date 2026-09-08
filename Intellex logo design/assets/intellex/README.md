# Intellex asset set

Mark: readout instrument (blue arc ring + tick scale + hex housing) with the existing R2 logo seated in the housing.
Wordmark: Chakra Petch 600, letterspaced 0.10em, `INTEL` in ink + `LEX` in blue.

## Colours
| Use | Dark UI | Light UI |
| --- | --- | --- |
| Accent / `LEX` | `#2E8FE0` | `#1A6FBD` |
| Ring | `#4AA6EF` | `#1A6FBD` |
| Housing outline | `#E4E9EE` | `#12151A` |
| Ink | `#F4F5F2` | `#12151A` |
| Ground | `#0D1015` | `#F4F5F2` |

## Files

### App icon
- `intellex.ico` — 16/24/32/48/64/256, PNG-compressed, for the Windows build
- `icon-1024.png` … `icon-16.png` — transparent PNGs
- `dock-tile-1024.png` — squircle tile; source for `.icns` (electron-builder / `iconutil` take the 1024 PNG)
- `apple-touch-icon-180.png`, `favicon-32.png`, `favicon-16.png`
- `icon-{64,128,256,512}-light.png` — white housing, darker blue, for light chrome

Three icon builds by size, on purpose: full instrument at 48px+, heavier ring with no ticks at 24–32px, R2 alone at 16px.

### Vector
- `intellex-mark.svg`, `intellex-mark-light.svg` — full instrument (R2 embedded as base64 raster)
- `intellex-mark-small.svg` — simplified small-size build

### Lockups (2x PNG)
- `lockup-dark.png`, `lockup-light.png`
- `lockup-dark-tagline.png`, `lockup-light-tagline.png`
- `wordmark-dark.png`, `wordmark-light.png`
- `splash-1600x900.png` — launcher header / splash

Descriptor line: `NAVICORE / WCB CONFIGURATION TOOL`, mono caps, 0.24em tracking.

## Clear space
Keep one ring-diameter of clear space around the mark; in the lockup, the gap between mark and wordmark is 26% of the mark's width.

## Known limit
The R2 art is a fixed-colour raster with a gradient, so the mark cannot invert to a single-colour silkscreen or print version. Supply the R2 as vector and the whole set can be regenerated flat and monochrome.
