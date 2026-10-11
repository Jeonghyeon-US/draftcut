# Frame-grid semantics

## Scope

The output timeline is a half-open window `[0, D)`. Its grid is defined by
`output_policy.frame_rate`; all time coordinates used to place that grid are
integer ticks in their declared timebase.

## Exact-distance ties

When two source frames are exactly equidistant from an output-grid point, the
displayed frame is the later source frame (the one after that grid point).

Historical development measurements of three sources found
134/300 ties (44.67%), 1/900 (0.11%), and 1/1800 (0.056%), respectively.
The source media, probe gate, and checksum records are not included in this
repository, so these observations cannot be reproduced here.

## Enforcement

Normalization expresses this policy with the `fps` filter argument `round=near`
in `executor/normalize.py`. The later-frame tie rule was observed during
development; this repository does not include the checksum probe that tested it.
The filter argument alone does not independently verify the displayed tie frame.
