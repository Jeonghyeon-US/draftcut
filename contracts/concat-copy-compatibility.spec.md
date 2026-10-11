# Concat-copy compatibility check specification

- `spec_id`: **`draftcut-concat-copy-compatibility`**
- `spec_version`: **`2`**
- Status: normative. This is the document that `EditPlan.concat_compatibility.spec_id`
 and `spec_version` name.

## 1. Purpose

The compatibility *conditions* are not contract values.
The plan carries a **verdict** (`compatible` / `incompatible` / `unverified`), its
**evidence**, and the **identity and version of the condition list it was judged
against**; the executor performs the checks. This is that condition list.

Record a verdict only after running the checks below. The `spec_id` and
`spec_version` identify the condition list evaluated; filling in those fields
without the checks does not establish compatibility.

Historical measurements below are observations made with the build used during
development. Results may differ between ffmpeg builds. The source media, build
configuration, and measurement records are not included in this repository, so
the reported numbers and hashes cannot be reproduced here. Probe each current
output rather than treating those observations as a guarantee.

**Version discipline.** Adding, removing, or changing a numbered condition in §4,
and any change to §5 or §6, **requires a new `spec_version`**. The already-enumerated
`output_policy.audio_policy` values select the two stream-set branches documented
below; `present` is not a new condition ID or a second compatibility specification.
A verdict produced against an older version does not become false, but it is no
longer a statement about the current list — which is precisely why the version is in
the contract. **Do not add or rewrite numbered §4–§6 conditions in place.**

## 2. Scope

**Applies to** a join whose `actions[].params.join_mode` is `concat_copy`: ffmpeg's
concat **demuxer** with `-c copy`, over MP4/H.264 pieces. With
`output_policy.audio_policy: none`, pieces are video-only. With `present`, every
piece has exactly one audio stream and the executor fixes it to AAC, 48 kHz, stereo,
192 kbit/s before the join.

**The unit of checking is the piece** — each file that will be listed in the concat
script, probed **before** the join. §4 R3 is the one post-join check.

**Out of scope for version 2**: other containers such as Matroska/WebM, other
video codecs such as HEVC, the concat *protocol* (`concat:`), the concat *filter*,
and arbitrary mid-stream cuts rather than complete encodes. The checker does not
distinguish pieces by who produced them: external MP4/H.264 pieces are judged
against the same policy and compatibility conditions. Encountering an unsupported
format yields `unverified`, never `compatible`; passing the probes does not verify
that an external piece was a complete encode.

## 3. Three kinds of condition — do not flatten them

The three are separate, and this is why "every value must be equal across pieces"
is the wrong shape and would build a wrong check:

| Class | Meaning | Here |
|---|---|---|
| **P — policy conformance** | the piece matches the plan's `output_policy` | §4 P1–P6 |
| **X — cross-piece identity** | the joined output carries **one** description, taken from the first piece, so a later piece that differs is silently misdescribed | §4 X1–X8 |
| **R — state and continuity** | not equality at all: each piece must *start* in a decodable state and the timeline must stay valid across the join | §4 R1–R3 |

**P and X are not redundant.** Policy checks verify that normalization applied
the declared geometry, pixel format, sample aspect ratio (SAR), colour, frame rate,
and timebase settings. Cross-piece checks detect differences not fully covered by
those settings, including decoder configuration data (`extradata`), regardless
of who produced a piece.

## 4. Conditions

Every condition names the observation command. `INPUT` is one piece. Commands are run
per piece and the outputs are compared. **All values are compared as exact strings or
exact rationals — never as floats.**

### Before P: the contract's names are not `ffprobe`'s names

**The P-class checks compare a contract value against a probe value, and neither the
key nor the value form matches. Getting this wrong is how a check silently compares
two things that can never be equal.**

| `output_policy` field | value form | `ffprobe` field | value form |
|---|---|---|---|
| `width`, `height` | integer | `width`, `height` | integer |
| `pixel_format` | string `"yuv420p"` | **`pix_fmt`** | string |
| `sar` | **rational** `{"num":1,"den":1}` | `sample_aspect_ratio` | **string `"1:1"`** — colon |
| `frame_rate` | **rational** `{"num":30,"den":1}` | `r_frame_rate`, `avg_frame_rate` | **string `"30/1"`** — slash |
| `encoder_timebase` | **rational** `{"num":1,"den":15360}` | `time_base` | **string `"1/15360"`** — slash |
| `color_primaries` | string | `color_primaries` | string |
| `color_transfer` | string | `color_transfer` | string |
| **`color_matrix`** | string | **`color_space`** | string |
| `color_range` | string | `color_range` | string |

**Two names differ (`pixel_format`/`pix_fmt`, `color_matrix`/`color_space`) and
three values are rationals on one side and strings on the other — with two different
separators.** **Parse the probe string into an exact rational and compare rationals**
— the same rule the Index contract already gives for time: never go through a float,
and never compare the formatted strings. `"1:1"` and `{"num":1,"den":1}` are the same
value written two ways; `1.0 == 1.0` is a different and weaker claim.

### P — conformance to `output_policy` (per piece)

| ID | Condition | Observe | Pass criterion |
|---|---|---|---|
| **P1** | frame geometry | `ffprobe -v error -show_optional_fields always -select_streams v:0 -show_entries stream=width,height,coded_width,coded_height -of json INPUT` | `width`/`height` equal `output_policy.width`/`height` |
| **P2** | pixel format | `… -show_entries stream=pix_fmt,bits_per_raw_sample -of json INPUT` | `pix_fmt` equals `output_policy.pixel_format` exactly. `yuvj420p` is **not** `yuv420p` |
| **P3** | sample aspect ratio | `… -show_entries stream=sample_aspect_ratio,display_aspect_ratio,width,height -of json INPUT` | `sample_aspect_ratio` equals `output_policy.sar` |
| **P4** | colour tags | `ffprobe -v error -show_optional_fields always -select_streams v:0 -show_entries stream=color_primaries,color_transfer,color_space,color_range -of json INPUT` | all four equal the `output_policy` values. `unknown` is a failure, not a pass. **`-show_optional_fields always` is mandatory here** — without it an `unknown` field is simply omitted from the JSON, and a check that reads "the key is absent" as "nothing to compare" turns the failure into a pass |
| **P5** | frame rate and CFR | `ffprobe -v error -select_streams v:0 -show_entries stream=r_frame_rate,avg_frame_rate -of json INPUT` **and** `ffprobe -v error -select_streams v:0 -show_packets -show_entries packet=pts,dts,flags -of json INPUT` | both rates equal `output_policy.frame_rate`, **and** every adjacent packet **DTS** delta is identical and equals `frame_rate⁻¹ ÷ time_base` (historical development observation: 512 ticks at 1/15360). This, not the declared rate, is what makes `frame_rate_mode: cfr` true |
| **P6** | timebase | `ffprobe -v error -select_streams v:0 -show_entries stream=time_base -of json INPUT` | equals `output_policy.encoder_timebase` |

**Historical P4 observation from the development build.**
The colour-options-only normalisation command —
`-color_primaries bt709 -color_trc bt709 -colorspace bt709 -color_range tv` — produced
output where **`color_space` and `color_range` are set and `color_primaries` and
`color_transfer` came back `unknown`.** This was observed on three sources,
including the HDR one; `-movflags +write_colr` did not change that development
result. In that build, the colour-options-only output did not retain those tags.
This observation does not establish how other builds propagate encoder metadata.
**Adding `-x264-params colorprim=bt709:transfer=bt709:colormatrix=bt709` wrote all
four in that development build**, and the three sources then converged on one 49-byte extradata
`SHA256:7aba4b2f…387c`.
The current executor supplies both the colour options and `-x264-params`;
P4 checks the resulting tags. The historical `80aaa9fa…ad40` hash belongs to the
colour-options-only observation, not the correctly tagged pieces from that test.

**Historical P6 observation from the development build.** A join of mismatched
timebases interpreted packet timestamps under the wrong unit — 2s + 2s
became 5.311111s in that test. This is why P6 is a hard equality against the policy value rather
than a cross-piece comparison alone: matching each other but not the policy would
still leave the executor's `-enc_time_base` unstated.

### X — cross-piece identity (compare every piece to the first)

| ID | Condition | Observe | Pass criterion |
|---|---|---|---|
| **X1** | stream set | `ffprobe -v error -show_entries stream=index,codec_type -of json INPUT` | with `audio_policy: none`, exactly one video stream at index 0; with `present`, exactly one video and one audio stream. audio present anywhere is a failure while `audio_policy` is `none` |
| **X2** | codec | `ffprobe -v error -select_streams v:0 -show_entries stream=codec_name,codec_tag_string -of json INPUT` | identical across pieces |
| **X3** | decoder configuration data (`extradata`) | `ffprobe -v error -select_streams v:0 -show_entries stream=extradata_size,extradata_hash -show_data_hash sha256 -of json INPUT` | `extradata_size` and `extradata_hash` identical across pieces |
| **X4** | profile / level | `ffprobe -v error -select_streams v:0 -show_entries stream=profile,level -of json INPUT` | identical across pieces |
| **X5** | field order | `ffprobe -v error -show_optional_fields always -select_streams v:0 -show_entries stream=field_order -of json INPUT` | identical across pieces, and `progressive` |
| **X6** | chroma location | `ffprobe -v error -show_optional_fields always -select_streams v:0 -show_entries stream=chroma_location -of json INPUT` | identical across pieces |
| **X7** | stream side data | `ffprobe -v error -show_entries stream=index,codec_type:stream_side_data_list -of json INPUT` | empty in every piece, or byte-identical across pieces |
| **X8** | HDR static metadata | `ffprobe -v error -select_streams v:0 -show_frames -read_intervals %+#10 -show_entries frame=pts,pts_time:frame_side_data_list -of json INPUT` | after removing `H.26[45] User Data Unregistered SEI message` (see below): absent in every piece, or identical across pieces |

**Historical X7/X8 observation from the development build.** A high-dynamic-range
(HDR) source carrying mastering-display and content-light-level metadata
was pushed through the normalisation path **with colour tags forced**:
the colour tags were corrected to `bt709` and the extradata hash became
`SHA256:80aaa9fa…ad40` — the exact value measured from entirely different
sources in that test — **and the mastering-display and content-light-level side
data survived unchanged, identical to the control run.** The matching extradata
hash did not establish that side data was absent; X7 and X8 check it separately. **In the HEVC source the metadata appeared only at frame level
with an empty stream-level list; after normalisation it appeared at stream level.**
That is why both X7 and X8 exist and why neither may be dropped because the other
came back empty.

A missing `side_data_list` key can indicate that no side data is present; the
checker does not require an explicit empty list. In a historical development
observation, X7 returned `streams:[{index, codec_type}]` with no `side_data_list`
key, and X8 returned that key only on the first frame; the other 59 frame objects
lacked it. Requiring the key in every object would incorrectly mark those
observations as unevaluable.

**X8 excludes one side-data type by its literal name:**
`H.26[45] User Data Unregistered SEI message`. In the historical development
observations, this type appeared on the first frame of both normalised pieces.
The current checker removes this type and compares the remaining frame side
data; the exclusion does not suppress other types, including HDR metadata.

**X7/X8 use `stream_side_data_list` / `frame_side_data_list` separately.**
An empty stream-level result does not establish that frame-level side data is
absent. A historical development source had an empty stream-level list and
non-empty frame-level data, so neither check substitutes for the other.

**Historical normalization observation from the development build:** eight
deliberately dissimilar sources — CFR 30 and CFR 24 H.264, 10-bit, 4:2:2, HEVC,
interlaced (`tt`), portrait 360×640, and VFR — went through the identical
normalisation command with colour forced, and **all eight came out with the same
`profile` High, `level` 31, `pix_fmt` `yuv420p`, SAR `1:1`, `chroma_location` `left`,
`field_order` `progressive`, and the same 49-byte extradata SHA256.** ⇒ **X3–X6 are
expected to pass on those measured pieces**. Current results may differ by
build; X3–X6 still evaluate every in-scope multi-piece join.

**Why X3 remains a check despite `output_policy` prevention.** Matching colour
tags do not establish identical decoder configuration data. P4 checks colour
conformance; X3 separately compares `extradata_size` and `extradata_hash` across
pieces before joining.

### R — state and continuity

**Present-audio binding.** When `audio_policy` is `present`, the checker also probes
each selected audio stream and compares its codec, sample rate, channel count,
channel layout exactly across pieces. The normalizer also commands a fixed nominal
192 kbit/s target, but `ffprobe` derives AAC `bit_rate` from short-stream packet
accounting, so that aggregate is evidence only rather than a false exact-equality
criterion. This is bound to
`executor.concat_check._condition_audio_identity`; a mismatch is `incompatible`.
The final-output probe requires one audio stream and a duration within two AAC frames
(2048/48000 s) of the assembled piece duration. This is a mux/priming allowance, not
a silent fallback.

| ID | Condition | Observe | Pass criterion |
|---|---|---|---|
| **R1** | each piece opens at a random-access point | `ffprobe -v error -select_streams v:0 -show_packets -show_entries packet=pts,dts,flags -of json INPUT` | the first packet of every piece carries the `K` flag |
| **R2** | decode order is sane within a piece | same output as R1 | `dts` is strictly increasing across the piece, with the constant delta from P5 |
| **R3** | the join is continuous (**post-join**) | the same packet command, run on the concatenated output; with `audio_policy: present`, also the selected audio-stream `sample_rate` from each piece | output `nb_frames` equals the exact sum of the pieces' in both policies. In both policies, every **non-join** delta is the one constant delta exactly; joins are the cumulative piece-frame counts, and each join delta may differ only by **strictly less than** its policy-specific tolerance. With `audio_policy: none`, that tolerance is one video-frame interval (the expected constant delta itself): a boundary-only difference can arise from the container's final-sample-length metadata, which ffmpeg derives from the source cut timing and the concat demuxer carries into the next piece's DTS. This does not change frame order or decodability when it is below one frame. With `present`, the tolerance is one AAC frame: `1024 / actual_audio_sample_rate / video_timebase` ticks. The checker records every join's expected/actual/deviation/tolerance result and rejects any non-join deviation, tolerance-sized-or-larger join deviation, or frame-count mismatch. |

**What changed from version 1 to version 2.** The contract permits `audio_policy: none`
to have a strictly-less-than-one-video-frame DTS deviation at a piece join only. This
accounts for container final-sample-length metadata, not AAC frame granularity; every
non-join delta remains exact, and the frame-count check is unchanged.

**Why present-audio R3 cannot demand perfectly equal DTS intervals.** An AAC packet carries
1024 samples: at 48 kHz that is 21.333… ms, or 640 ticks in a 1/30000 video
timebase. A 30000/1001 video frame is 1001 ticks. Because 640 and 1001 do not
divide evenly, the concat demuxer may make a sub-AAC-frame video-DTS adjustment
at a piece boundary to preserve A/V synchronization. That adjustment is a
boundary-only mux effect, not a loss of CFR within either piece; requiring exact
equality there would reject conforming audio-bearing output. The tolerance is
computed from the pieces' ffprobe-measured sample rate, never a fixed 640-tick
literal.

**R2 and R3 use DTS (decode timestamps), not PTS (presentation timestamps).**
Packets come out in decode order, so with B-frames adjacent PTS deltas need not be
constant or monotonic. A historical development join read
`pts` 29184 → 28672 → 29696 → 30720 while `dts` runs 28160 → 28672 → 29184 → 29696 →
30208 → 30720, a clean +512 throughout, boundary included. **A criterion written on
`pts` would be one the execution path cannot satisfy**.

**Historical development baseline** (reported version: ffmpeg 8.1.2; build
configuration and reproduction materials are not included): two normalised
2-second pieces from deliberately dissimilar sources → concat exit 0 with no warning,
`time_base` 1/15360, 60 + 60 = **120** frames, `duration` **4.000000**, DTS delta 512
everywhere, and the `K` flag on the first packet of each piece.

**R1 is a proxy.** The checker tests the first packet's `K` flag; it does not
parse the bitstream to prove all random-access requirements. R1 applies only
within the complete-encode scope defined in §2. Historical development
measurements found `K` on the first packet of each normalised piece. Expanding
the scope to arbitrary mid-stream cuts or other codecs requires additional
checks and a new specification version.

## 5. What version 2 does **not** check

**This list explains why `unverified` exists and defines the limits of a
`compatible` verdict.**

| Not checked | Why |
|---|---|
| container `mvhd` / `mdhd` timescale, `elst` edit lists | **No structured `ffprobe` field exists** — could only observe them as `-v trace` strings. Checking them needs a separate parser. Our path emits one muxer default, so the risk is bounded, **not** absent |
| decoder-buffer timing state (HRD / CPB) and buffering-period messages (SEI) | additional bitstream checks would be needed; version 2 covers complete encodes rather than joins within a coded video sequence (CVS) |
| encoded-unit framing (NAL; Annex B vs length-prefixed) beyond the extradata hash | X3 compares decoder configuration data; full framing validation needs a bitstream parser |
| picture ordering and reference-picture continuity (POC / RPS) | these bitstream properties are outside the complete-encode checks; §2 excludes joins within a coded video sequence |
| audio priming/padding and `seek_preroll` internals | `present` checks equality of codec, sample rate, channel count, and channel layout, plus final duration. The encoder targets 192 kbit/s; measured `bit_rate` is evidence only, not an exact-equality condition. Codec-delay side data is not parsed |
| content encryption / `ContentEncodings`, container brands and offsets | not produced by our path |
| whether the rendered pixels are correct | this spec judges joinability, not quality. That is QC |

**Do not read this table as a to-do list.** Each row is a scope statement; moving
one into §4 is a new `spec_version` with a measurement behind it.

## 6. Verdict rules

| Verdict | When |
|---|---|
| **`compatible`** | every condition in §4 that is in scope for the plan was **evaluated** and **passed**, on **every** piece |
| **`incompatible`** | any §4 condition was evaluated and **failed** |
| **`unverified`** | any §4 condition could **not** be evaluated — a probe command failed, a field came back missing or empty, a piece could not be read, the pieces are in a format §2 does not cover, or the checks were run against a different `spec_version` than the one recorded |

- **`unverified` is not a soft pass.** The contract makes it block `ready_for_render`
 and `rendered`, because the entire problem is that `concat` + `-c copy` exits 0 on
 mismatched pieces with no warning. "Not measured" must never read as "passed".
- **`incompatible` forbids render** through the existing `review(blocking)` gate —
 `concat_compatibility.review_id` must name a blocking review. **No second
 blocking mechanism is introduced.**
- **A single-piece plan** has no join: X1–X8 and R3 are vacuous, P1–P6 and R1–R2 still
 apply, and the verdict may be `compatible`.
- **A decode-time warning is not a check.** Historical development measurements
 recorded `[swscaler] deprecated pixel format used…` for a `yuvj420p` mismatch,
 while SAR and colour-space mismatches produced no warning. Those observations
 are specific to the development build. The checker must evaluate the specified
 conditions; absence of ffmpeg warnings is not evidence of compatibility.

## 7. Evidence

`concat_compatibility.evidence_ids` must point at records that carry, for the run that
produced the verdict:

1. `ffmpeg` and `ffprobe` version strings,
2. this `spec_id` and `spec_version`,
3. the ordered list of pieces with each one's SHA-256,
4. the **raw output** of every command in §4 for every piece, and
5. for R3, every join's `join_index`, `expected_delta`, `actual_delta`,
  `deviation_ticks`, `tolerance_ticks`, and `within_tolerance`, plus every
  non-join violation index. With `audio_policy: none`, the tolerance basis is
  one video-frame interval and container final-sample-length metadata; with
  `present`, it is the AAC-samples/sample-rate/video-timebase derivation, and
6. for `incompatible`, which condition ID failed and both differing values.

**4 is what makes the verdict independently re-checkable.** A verdict recorded as a
summary is a claim; a verdict recorded with its probe output is a measurement.

## 8. Version history

| Version | Change |
|---|---|
| `1` | First condition list. Its historical development measurements cannot be reproduced from this repository. |
| `2` | R3 permits a strictly-less-than-one-video-frame DTS deviation at joins when `audio_policy: none`, accounting for container final-sample-length metadata. Non-join deltas remain exact and frame counts unchanged; the present-audio join tolerance remains one AAC frame. |
