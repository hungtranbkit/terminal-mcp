# Release card

## Anatomy
Container (surface.card), heading (h2), body text (text.secondary).

## Variants
Default only. A highlighted variant is out of scope for this fixture.

## States
Default, hover (no change: the card is not interactive), focus-within when it contains a link.

## Token mapping
Background `semantic.surface.card`, border `semantic.border.default`, radius `--radius-lg`, padding `spacing --space-4`.

## Accessibility
Each card is an `article` with an `h2`; contrast of text.secondary on surface.card meets WCAG 2.2 AA.
