# Create-agent wizard: measured baseline (2026-08-27, viewport 1440x900)

Measured in a real browser via Playwright against the running dev servers.

| Measurement | Value |
|---|---|
| Drawer panel width | 544px (`sm:w-[34rem]`) |
| Usable content width | **511px** of a 1440px viewport (35%) |
| Step 2 persona cards | 9 cards at **159px wide** each (`lg:grid-cols-3` inside a 544px box) |
| Persona titles wrapping to 2 lines | 4 of 4 sampled |
| Persona descriptions truncated by line-clamp | **6 of 9** |
| Step 2 horizontal overflow | **40px** (grid 557px in a 501px box) |
| Step 2 hidden below fold | 713px (1603 scroll / 890 visible) |
| Step 3 Customize sliders | 7 at **239px wide** |
| Longest help text under a 239px slider | 296 chars = **10 rendered lines** |
| Step 3 horizontal overflow | **49px** |
| Step 3 vertical scrolling | **2.5 screens** (2225px scroll / 890 visible) |
| Chunk-size number input vs Overlap label | **31px collision** -> renders as "800Overlap" |
| Overlap number input right edge | 1477px = **37px outside the 1440px panel edge** |

## Root cause
Viewport breakpoints (`sm:`, `lg:`) inside a fixed-width 544px container. Tailwind's
breakpoints ask "how wide is the window", never "how wide is my box", so on a 1440px
desktop `lg:grid-cols-3` and `sm:grid-cols-4` all fire inside a 511px column.

CLAUDE.md states "zero horizontal scroll at 320px" as a hard requirement. This ships
with horizontal scroll at 1440px.
