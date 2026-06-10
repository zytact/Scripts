---
name: Agent Usage Report
description: A dense local usage dossier for coding-agent activity.
colors:
  bg: "oklch(0.075 0 0)"
  surface: "oklch(0.135 0.010 258)"
  surface-2: "oklch(0.175 0.014 258)"
  line: "oklch(0.285 0.020 258)"
  ink: "oklch(0.940 0.010 258)"
  muted: "oklch(0.720 0.018 258)"
  soft: "oklch(0.560 0.030 258)"
  primary: "oklch(0.681 0.132 258.4)"
  accent: "oklch(0.760 0.150 70)"
  input: "oklch(0.690 0.130 300)"
  cache: "oklch(0.760 0.115 205)"
  output: "oklch(0.780 0.145 82)"
  total: "oklch(0.681 0.132 258.4)"
typography:
  display:
    fontFamily: "ui-sans-serif, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif"
    fontSize: "2.75rem"
    fontWeight: 800
    lineHeight: 1.02
    letterSpacing: "-0.025em"
  body:
    fontFamily: "ui-sans-serif, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif"
    fontSize: "15px"
    fontWeight: 500
    lineHeight: 1.55
  label:
    fontFamily: "ui-sans-serif, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif"
    fontSize: "0.82rem"
    fontWeight: 700
    lineHeight: 1.2
rounded:
  none: "0px"
spacing:
  sm: "8px"
  md: "16px"
  lg: "24px"
  xl: "30px"
components:
  report-panel:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.none}"
    padding: "24px"
  metric-cell:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.none}"
    padding: "18px"
---

# Design System: Agent Usage Report

## 1. Overview

**Creative North Star: "Pre-dawn flight deck"**

This system is a tactical report surface for local agent activity. It uses a near-black architectural ground, cobalt instrumentation, and amber attention marks to make dense data readable without becoming a terminal costume.

The layout is compact and segmented. Panels feel like joined pieces of one instrument board, divided by fine tonal lines instead of floating cards or decorative shadows.

**Key Characteristics:**
- Dense but scannable.
- Dark, precise, and low glare.
- Cobalt primary with amber emphasis.
- Flat at rest, with depth created through tonal layers.
- Product UI first, visual identity second.

## 2. Colors

The palette is restrained: pure near-black base, cool cobalt primary, amber accent, and a small semantic token set for token buckets.

### Primary
- **Cobalt Instrument Glow** (`oklch(0.681 0.132 258.4)`): primary trace color for report identity, total tokens, and core emphasis.

### Secondary
- **Amber Attention Mark** (`oklch(0.760 0.150 70)`): used sparingly for range labels, warnings, and source context.

### Tertiary
- **Input Violet** (`oklch(0.690 0.130 300)`): input token bars only.
- **Cache Cyan** (`oklch(0.760 0.115 205)`): cached token bars only.
- **Output Ochre** (`oklch(0.780 0.145 82)`): output token bars only.

### Neutral
- **Black Deck** (`oklch(0.075 0 0)`): page background.
- **Panel Steel** (`oklch(0.135 0.010 258)`): primary panel surface.
- **Raised Panel Steel** (`oklch(0.175 0.014 258)`): nested detail panels.
- **Instrument Line** (`oklch(0.285 0.020 258)`): dividers and panel borders.
- **Readable Ink** (`oklch(0.940 0.010 258)`): body text and primary numbers.
- **Muted Telemetry** (`oklch(0.720 0.018 258)`): secondary labels and explanatory copy.

### Named Rules

**The No Purple SaaS Rule.** Purple gradients are prohibited. Cobalt is a functional instrument color, not a gradient decoration.

**The One Accent Rule.** Amber appears only where the eye needs to land. If it covers more than a few labels or badges, the report is too loud.

## 3. Typography

**Display Font:** system UI sans stack.
**Body Font:** system UI sans stack.
**Label/Mono Font:** system UI sans stack with tabular numerals where data alignment matters.

**Character:** Familiar product typography. No terminal face, no novelty mono, no display drama inside data panels.

### Hierarchy
- **Display** (800, 2.75rem, 1.02): report title only.
- **Headline** (800, 2rem, 1.05): source active-time figures.
- **Title** (750, 1rem, 1.25): panel titles and section labels.
- **Body** (500, 15px, 1.55): report descriptions and explanatory copy, capped around 72ch.
- **Label** (700, 0.82rem, 1.2): metric labels, source labels, and compact UI captions.

### Named Rules

**The No Terminal Type Rule.** Do not use monospace as the primary voice. Tabular numerals are allowed for alignment, but the report must not look like an ANSI terminal dump.

## 4. Elevation

The report is flat by default. Depth comes from tonal layering, 1px boundaries, and joined grid seams. Shadows are intentionally absent so the dashboard feels local, crisp, and printable.

### Named Rules

**The Joined Surface Rule.** Related data should connect edge to edge with shared dividers. Do not scatter unrelated floating cards across the canvas.

## 5. Components

### Buttons

No persistent buttons are part of the current report. If added later, use square or near-square corners, cobalt fill for the primary action, readable white text, and a visible focus outline.

### Chips

Use filled or bordered chips only for filters or source state. Keep labels short and avoid saturated inactive states.

### Cards / Containers

- **Corner Style:** square, no rounding (`0px`).
- **Background:** panel steel for primary sections, raised panel steel for detail cells.
- **Shadow Strategy:** none.
- **Border:** 1px instrument line.
- **Internal Padding:** 18px for metric cells, 22px to 24px for panels, 30px for the report hero.

### Inputs / Fields

No input fields are part of the current static report. If added later, use panel steel background, instrument line border, cobalt focus ring, and readable placeholder text.

### Navigation

No navigation is part of the static report. Repeated source sections serve as the navigation rhythm: Combined, Codex, Claude Code when enabled, and Pi.

### Token Bars

Token bars are the signature component. Each row has a fixed label column, a proportional track, and a tabular numeric value. Input, cached, output, and total always use their assigned colors.

## 6. Do's and Don'ts

### Do:

- **Do** keep the report dense, with clear seams and compact rank lists.
- **Do** use OKLCH tokens directly in CSS.
- **Do** reserve amber for labels and attention marks.
- **Do** keep estimates honest: missing pricing data must read as `n/a`.
- **Do** keep the HTML standalone, with no external fonts, scripts, or assets.

### Don't:

- **Don't** use generic SaaS card grids, purple gradients, decorative glass panels, terminal-type novelty, console/ANSI clones, or overly playful marketing visuals.
- **Don't** use decorative shadows or frosted panels.
- **Don't** add rounded 32px cards or pill-shaped report panels.
- **Don't** use gradient text.
- **Don't** let long model names overflow their container.
