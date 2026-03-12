# Design System

A portable design system for building warm, data-rich dashboard interfaces.
Built for Jinja2 + Alpine.js + ApexCharts — no build tools required.

---

## Color Palette

Light theme with warm neutrals. Never pure white, never pure black.

### Core Surfaces

| Token | Hex | Usage |
|---|---|---|
| `--page-bg` | `#F5F0E8` | Page background — warm sand |
| `--card-dark` | `#1A1D2E` | Hero KPI cards, high-contrast elements |
| `--card-light` | `#FAF8F6` | Default card surface — warm cream |
| `--card-tinted` | `#F0E8D8` | Medium-priority content, secondary cards |
| `--card-green` | `#E6F5EC` | Positive metrics, wins, success states |

### Accent & Semantic Colors

| Token | Hex | Usage |
|---|---|---|
| `--accent` | `#D4870E` | Primary accent — amber gold. CTAs, active nav, chart series 1 |
| `--accent-dim` | `rgba(212,135,14,.10)` | Accent tint backgrounds |
| `--accent-mid` | `rgba(212,135,14,.18)` | Accent hover/active backgrounds |
| `--green` | `#34D399` | Success, positive change, replies, chart series 2 |
| `--blue` | `#60A5FA` | Info, opens, queue counts |
| `--amber` | `#F59E0B` | Warning, pending actions, follow-ups |
| `--rose` | `#FB7185` | Error, negative, lost deals |
| `--teal` | `#2DD4BF` | Tertiary highlight |

### Text Colors

| Token | Hex | Usage |
|---|---|---|
| `--tx1` | `#1E2132` | Primary text — headings, values |
| `--tx2` | `#5C607A` | Secondary text — body, descriptions |
| `--tx3` | `#8E92AB` | Tertiary — labels, captions, disabled |

### Borders & Shadows

| Token | Value | Usage |
|---|---|---|
| `--border` | `#E0D8CA` | Card borders, dividers |
| `--border-l` | `#CCBFAA` | Hover borders, active states |
| `--shadow-sm` | `0 1px 3px rgba(26,29,46,.06), 0 1px 2px rgba(26,29,46,.04)` | Cards, pills |
| `--shadow-md` | `0 4px 16px rgba(26,29,46,.08), 0 2px 4px rgba(26,29,46,.04)` | Tooltips, dropdowns |
| `--shadow-lg` | `0 10px 40px rgba(26,29,46,.10), 0 4px 12px rgba(26,29,46,.06)` | Dark cards, modals |

---

## Typography

Font: **Inter** (Google Fonts), all weights 300-900.

### Scale

| Class | Size | Weight | Tracking | Use |
|---|---|---|---|---|
| `.text-hero` | 56px | 900 | -0.04em | Hero numbers — total leads, primary KPI |
| `.text-kpi` | 40px | 800 | -0.03em | Secondary KPIs — wins, rates |
| `.text-metric` | 28px | 800 | -0.02em | Inline metrics — card values |
| `.text-label` | 11px | 600 | 0.08em | Labels — uppercase, muted. Used above every number |
| `.text-body` | 14px | 400 | normal | Body text |

### Rules

- Numbers are always heavier than their labels (800-900 vs 400-600)
- Labels are always uppercase with letter-spacing
- No font size between 14px body and 28px metric — skip the middle
- Hero numbers should feel oversized; they anchor the visual hierarchy
- Line-height: 1 for numbers, 1.5 for body text

---

## Layout

### Structure

```
┌──────┬─────────────────────────────────────┐
│      │                                     │
│  64  │         Main Content Area           │
│  px  │         max-width: 1400px           │
│      │         padding: 28px 36px          │
│ side │                                     │
│ bar  │                                     │
│      │                                     │
└──────┴─────────────────────────────────────┘
```

- **Sidebar**: 64px wide, fixed left, dark (`--card-dark`), icon-only navigation
- **Main content**: `margin-left: 64px`, max-width 1400px, padded
- **Mobile (< 768px)**: Sidebar collapses to 0, content goes full-width

### Bento Grid

Use CSS Grid with asymmetric column ratios. Never a uniform grid.

```css
/* Hero row — dominant + secondary */
grid-template-columns: 2fr 1fr;

/* Equal cards */
grid-template-columns: repeat(3, 1fr);

/* KPI strip — 5 equal */
grid-template-columns: repeat(5, 1fr);

/* Content + sidebar widget */
grid-template-columns: 1.2fr 1fr;
```

Gap: 14-16px between cards. Margin-bottom: 20-24px between rows.

### Responsive Breakpoints

| Width | Behavior |
|---|---|
| > 1100px | Full bento layout |
| 768–1100px | Collapse multi-column to single column |
| < 768px | Sidebar hidden, single column, tighter padding |
| < 700px | KPI strip drops to 2-col |

---

## Card Variants

Four card types create visual depth and hierarchy.

### `.card-dark`

```css
background: #1A1D2E;
border-radius: 24px;
color: #fff;
box-shadow: 0 10px 40px rgba(26,29,46,.10), 0 4px 12px rgba(26,29,46,.06);
```

Use for: Hero KPIs, primary metrics that need to jump off the page.
Text inside: white. Labels: `rgba(255,255,255,.45)`. Sub-text: `rgba(255,255,255,.5)`.

### `.card-light`

```css
background: #FAF8F6;
border-radius: 20px;
box-shadow: 0 1px 3px rgba(26,29,46,.06), 0 1px 2px rgba(26,29,46,.04);
border: 1px solid rgba(224,216,202,.5);
```

Use for: Tables, charts, distribution lists, most content cards.
The default — use this when nothing else fits.

### `.card-tinted`

```css
background: #F0E8D8;
border-radius: 20px;
box-shadow: 0 1px 3px rgba(26,29,46,.06), 0 1px 2px rgba(26,29,46,.04);
```

Use for: Secondary panels, pipeline health, alternate campaign cards.
Creates visual variety without demanding attention.

### `.card-green`

```css
background: #E6F5EC;
border-radius: 20px;
box-shadow: 0 1px 3px rgba(26,29,46,.06), 0 1px 2px rgba(26,29,46,.04);
```

Use for: Wins, conversion metrics, positive-only KPIs.
Green text inside: `#0d9f52` (darker green for readability on green bg).

---

## Components

### Sidebar Navigation

64px icon-only sidebar on `--card-dark` background.

```html
<nav id="sidebar">
  <div class="sb-logo">O</div>      <!-- 36x36 accent square -->
  <div class="sb-nav">
    <a href="/" class="sb-link active">
      <svg>...</svg>
    </a>
  </div>
  <div class="sb-bottom">...</div>   <!-- sync, auth -->
</nav>
```

Active state: `rgba(212,135,14,.25)` bg + 3px accent bar on left edge.
Icons: 20x20 SVG, stroke-based (Feather/Lucide style).

### Pills & Badges

```html
<!-- Default (accent) -->
<span class="pill"><span class="live-dot"></span> Live</span>

<!-- Colored variants -->
<span class="pill pill-green">Active</span>
<span class="pill pill-blue">Email</span>
```

Pill: 99px border-radius, 5px 14px padding, 11px/600 text.
Always: tinted background + matching text + subtle border.

### Filter Pills

```html
<div class="filter-pills">
  <button class="filter-pill active">All</button>
  <button class="filter-pill">Contacted</button>
</div>
```

Active state: solid accent background, white text.
Inactive: transparent bg, border, muted text.

### Status Badges

Inline badges for pipeline status in tables.

```
new:       bg rgba(142,146,171,.15)  text #5C607A
queued:    bg rgba(96,165,250,.15)   text #60A5FA
contacted: bg rgba(52,211,153,.15)   text #0d9f52
replied:   bg rgba(245,158,11,.15)   text #d97706
won:       bg rgba(212,135,14,.15)   text #D4870E
lost:      bg rgba(251,113,133,.15)  text #FB7185
```

10px uppercase, 700 weight, 8px border-radius.

### Buttons

```html
<button class="btn btn-primary">Sign in</button>    <!-- accent bg, white text -->
<button class="btn btn-ghost">Cancel</button>        <!-- light bg, border, muted -->
<button class="btn btn-ghost-dark">...</button>       <!-- for use on dark surfaces -->
```

Border-radius: 10px. Padding: 7px 16px. Font: 12px/600.

---

## Charts (ApexCharts)

### Theme Object

```javascript
const chartTheme = {
  chart: { background: 'transparent', fontFamily: 'Inter, system-ui, sans-serif' },
  grid: { borderColor: 'rgba(224,216,202,.4)', strokeDashArray: 3 },
};
```

### Color Assignments

| Series | Color | Meaning |
|---|---|---|
| Primary | `#D4870E` | Outreach / sent / activity |
| Secondary | `#34D399` | Replies / success |
| Tertiary | `#60A5FA` | Opens / info |

### Area Charts

```javascript
stroke: { curve: 'smooth', width: [3, 2.5] },
fill: {
  type: 'gradient',
  gradient: { shadeIntensity: 0.1, opacityFrom: 0.3, opacityTo: 0.02, stops: [0, 95] }
},
```

- Always gradient fill, fading to near-transparent
- Smooth curves, no data labels
- X-axis: 10px, muted color, rotated -45 when dense
- Tooltip: `theme: 'light'` (matches card-light surface)

### Donut Charts

```javascript
stroke: { width: 2, colors: ['#FAF8F6'] },  // card-light separator
plotOptions: {
  pie: { donut: { size: '68%' } }
}
```

### Sparklines

Height 80px, single-color, no tooltip. Used inside performance cards.

---

## Page Templates

### Overview (cross-entity summary)

```
Hero row:      2fr + 1fr  (card-dark KPIs)
Entity cards:  3-col      (card-light / card-tinted mix)
Bottom row:    2fr + 1fr  (card-light chart + card-green wins)
```

### Detail (single entity deep-dive)

```
Header:        Title + selector pills
KPI strip:     5-col (dark + tinted + green + light + light)
Hero row:      1.7fr + 1fr (card-light perf + card-tinted pipeline)
Chart:         full-width card-light
Mid row:       1.2fr + 1fr (card-light table + card-tinted stages)
Bottom row:    1fr + 1fr + 1.5fr (distributions + leads table)
```

### CRM / Data Entry

```
KPI strip:     5x flex (mixed card types)
Filters:       Toggle + search + pill bar
Two-column:    Table (card-light) + Detail panel (card-light)
```

---

## Interaction Patterns

### Hover States

- Cards: `transform: translateY(-2px)` + `box-shadow: var(--shadow-md)` on campaign cards
- Table rows: tinted background (`var(--card-tinted)`)
- Buttons: `brightness(1.1)` for primary, border darken for ghost
- Links: underline on hover, accent color

### Active / Selected States

- Sidebar: accent-tinted bg + left bar indicator
- Filter pills: solid accent bg + white text
- Table rows: `var(--accent-dim)` background

### Transitions

- All interactive elements: `transition: all 140-180ms`
- Progress bars: `transition: width .6s ease`
- Live dot: `animation: pulse 2.4s infinite`

### Auto-refresh

5-minute countdown, silent page reload. No visible countdown UI.

---

## Customization Guide

### Swapping the Accent Color

Change one CSS variable and 2-3 hardcoded rgba values:

1. `--accent: #D4870E` → your color
2. `--accent-dim: rgba(R,G,B,.10)` → 10% opacity version
3. `--accent-mid: rgba(R,G,B,.18)` → 18% opacity version
4. Sidebar active: `rgba(R,G,B,.25)`
5. Pill border: `rgba(R,G,B,.2)`
6. Chart `colors[]` array: first value

### Swapping the Background Warmth

To go cooler (blue-gray tone):
- `--page-bg: #F0F2F5`
- `--card-tinted: #E4E8F0`
- `--border: #D4D8E4`
- `--border-l: #BCC2D2`

To go warmer (current — sand/gold tone):
- `--page-bg: #F5F0E8`
- `--card-tinted: #F0E8D8`
- `--border: #E0D8CA`
- `--border-l: #CCBFAA`

### Adding a New Card Variant

Follow the pattern:

```css
.card-[name] {
  background: [tinted-bg];
  border-radius: 20px;
  box-shadow: var(--shadow-sm);
}
```

---

## Tech Stack

| Layer | Tool | Notes |
|---|---|---|
| Templates | Jinja2 | Server-rendered, block inheritance |
| Interactivity | Alpine.js 3 | `x-data`, `x-for`, `x-show` — no build step |
| Charts | ApexCharts | CDN, imperative JS init |
| Font | Inter | Google Fonts CDN, wght 300-900 |
| Icons | Inline SVG | Feather/Lucide style, 20x20, stroke-2 |

No build tools, no bundler, no npm. Everything from CDN or inline.
