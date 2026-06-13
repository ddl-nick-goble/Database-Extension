# Domino Databases — logo assets

Locked design: **"Flatter Top, Deep Shade"** — a two-stack isometric database with the
Domino pinwheel laid flat on the lid.

Colors: body `#C24E1B`, top face `#E8642A` (brand orange), mark + seam `#ffffff`.

## Files

| File | Use |
| --- | --- |
| `logo.svg` | Master vector. Use anywhere you can render SVG. |
| `DominoDatabasesLogo.tsx` | React/TSX component, color + size props. |
| `icon-16…512.png` | Transparent-background PNGs at standard sizes. |
| `apple-touch-icon.png` | 180×180, orange background (iOS home screen). |
| `maskable-192.png`, `maskable-512.png` | Android/PWA maskable icons (safe padding). |
| `favicon.ico` | Multi-res favicon (16/32/48). |
| `site.webmanifest` | PWA manifest referencing the icons. |

## React usage

```tsx
import DominoDatabasesLogo from "./DominoDatabasesLogo";

<DominoDatabasesLogo size={40} />
// override colors if needed:
<DominoDatabasesLogo size={64} bodyColor="#C24E1B" topColor="#E8642A" markColor="#fff" />
```

Or import the SVG directly (Vite/CRA/Next all support this):

```tsx
import logoUrl from "./logo.svg";        // as a URL
<img src={logoUrl} alt="Domino Databases" width={40} />
```

## HTML head (favicon + PWA)

```html
<link rel="icon" href="/favicon.ico" sizes="any" />
<link rel="icon" type="image/png" href="/icon-32.png" sizes="32x32" />
<link rel="apple-touch-icon" href="/apple-touch-icon.png" />
<link rel="manifest" href="/site.webmanifest" />
<meta name="theme-color" content="#E8642A" />
```

Drop the PNGs, `favicon.ico`, and `site.webmanifest` into your app's `public/` folder,
and put `logo.svg` + `DominoDatabasesLogo.tsx` wherever your components live.

> Note: orange is `#E8642A`, matched from the reference. If Domino's official brand
> hex differs, update it in `logo.svg`, `DominoDatabasesLogo.tsx`, `site.webmanifest`,
> and regenerate the PNGs.
