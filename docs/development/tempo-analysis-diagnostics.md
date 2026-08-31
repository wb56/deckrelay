# Tempo analysis, planning and diagnostics

## Accepted production baseline

The real-music acceptance of `ffmpeg-onset-acf-v0.5` with `tempo-profile-v3` and
database schema 41 is complete. No further algorithm changes are planned for this
feature block. Full-recording and effective-cue analyses are stored independently,
cue changes invalidate affected cue results, and only a safe result becomes an
automatic planning value. Older v0.3 and v0.4 diagnostic reports remain readable.

The acceptance used six reference titles in FLAC, VBR MP3 and 320-kbit/s CBR MP3.
Each file was analyzed as a full recording and in its effective cue range, for a total
of 36 real analysis runs. Identical absolute ranges were additionally used for targeted
cross-format comparisons of stable musical sections. No reference audio or private
storage path is part of the repository.

### Positive references

| Reference | Runs | Observed result | Acceptance |
| --- | ---: | --- | --- |
| Daft Punk – Around the World | 6 | 121.212 BPM, 96.75% aggregate confidence, 100% rhythm stability, 242.424 BPM alternative and 0.00 BPM cross-format deviation | All runs `HIGH_CONFIDENCE` and automatically plannable |
| AC/DC – Back in Black | 6 | 92.159–92.380 BPM, 90.2–96.2% aggregate confidence, about 73.6% rhythm stability, complete family consensus and at most 0.14 BPM cross-format deviation | Stable real drums accepted without reporting artificial 100% stability |
| Dire Straits – Money for Nothing | 6 | The long, rhythmically sparse intro weakens the full-track evidence; after the common musical Cue In, all 15 sampled windows identify the family around 133.333 BPM across the three formats | The reliable cue result remains an independent scope and can safely supply the planning value |
| Queen – Another One Bites the Dust | 6 | The distributed full and cue analyses preserve the main family around 109–110 BPM despite reduced break sections and the fade-out | Break and fade-out handling accepted; moving Cue Out does not imply that every internal sample window moves |

### Critical references

| Reference | Runs | Observed result | Acceptance |
| --- | ---: | --- | --- |
| Toto – Rosanna | 6 | Correct family around 83/166 BPM in all formats; identical controlled ranges differ by at most 0.345 BPM while weak 21/42, 55–58/109–115 and other subdivision interpretations remain separate | Weak or conflicting runs require review and produce no reliable automatic planning value |
| Queen – Bohemian Rhapsody | 6 | Section-dependent proposals, 36.8–52.3% aggregate confidence and 28.2–56.7% rhythm stability | All runs report `DIFFERENT_TEMPO_FAMILIES`; genuine tempo changes are blocked in every format |

The acceptance therefore demonstrates the intended separation: constant electronic
material, stable natural drums and a reliable cue section after a sparse intro are
released. Breaks and fade-outs preserve the main family without hiding the distributed
sampling strategy. Weak shuffle/half-time evidence and genuine tempo changes remain
blocked. FLAC, VBR MP3 and 320-kbit/s CBR MP3 behave consistently for these decisions.

Together, the six references cover the accepted real-music risk corpus:

| Category | Reference | Accepted behavior |
| --- | --- | --- |
| Constant electronic beat | Daft Punk – Around the World | Format-independent, automatically plannable positive control |
| Rock/pop with natural drums | AC/DC – Back in Black | Natural timing spread is tolerated and the half/double family is retained |
| Quiet or rhythmically sparse intro | Dire Straits – Money for Nothing | Full and cue scopes remain separate; the cue section removes the weak intro evidence |
| Break and fade-out | Queen – Another One Bites the Dust | The main pulse is retained; a weak cue run remains safely blocked |
| Syncopation, shuffle and half-time | Toto – Rosanna | The correct family is found, but conflicting evidence is not promoted to a safe value |
| Genuine tempo changes | Queen – Bohemian Rhapsody | Ambiguous section-dependent proposals are blocked in every format |

DeckRelay analyzes the full recording and the effective catalog cue range as separate,
versioned scopes. Their persisted results and stale-state handling remain independent.
The visible Cue In and Cue Out values describe the requested musical range; they are not
the decoded excerpts used by the sampling algorithm.

## Sampling and tempo families

The `ffmpeg-onset-acf-v0.5` backend with `tempo-profile-v3` decodes mono PCM at
11,025 Hz and derives a 100 Hz
onset envelope. Tracks shorter than 30 seconds are analyzed as one range. Medium tracks
use three distributed excerpts of up to 30 seconds. Tracks of at least 150 seconds use
five distributed excerpts of up to 18 seconds, including early and late sections. This
improves temporal coverage for medleys and genuine tempo changes while keeping decoded
audio bounded.

Aggregation treats only half- and double-tempo interpretations as one family. Third-pulse
relations are deliberately not merged because they can make unrelated families such as
84 and 112 BPM appear compatible. Low family agreement produces a tempo-change warning.
Confidence describes the reliability of the aggregate BPM proposal. Local ACF confidence
is conservative and is calibrated from zero at 0.15 to full quality at 0.45. The median
calibrated quality prevents one good excerpt from dominating several weak excerpts.
Family consensus combines weighted agreement and the proportion of matched windows.
Aggregate confidence is 65% family consensus plus 35% calibrated median window quality.
Windows with local confidence of at least 0.20 are reported as usable, but their count
does not bypass the aggregate formula. Rhythm stability remains a separate temporal
measure based on family coverage, relative range and median absolute deviation. Thus a
family may be confidently identified while still varying over time.

Observed local-confidence medians in the neutralized real acceptance diagnostics explain
the calibration: constant electronic material was about 0.65–0.68, stable real drums
about 0.38–0.40, shuffle material about 0.16–0.20 and genuine multi-section material
about 0.12–0.19. Replaying the stored raw windows through v0.5 yields approximately
0.97 aggregate confidence for the constant electronic fixture, 0.94 for the stable-drum
fixture, 0.42 for shuffle and 0.41 for the multi-section fixture. The latter two remain
blocked independently by rhythm stability as well.
Family matching permits at most 8% relative deviation. Stability degrades continuously
with normalized range (25% scale) and median deviation (8% scale), rather than changing
at a title-specific threshold.
0.55 through 0.79 requires review; 0.80 and above is high confidence. A high confidence
alone is not reliable when rhythm stability is below 0.65.

## Planning values

Manual catalog and confirmed saved-queue BPM values retain their established priority.
Without a manual value, a current cue result is used only when confidence is at least
0.80 and no tempo-family instability is present. Otherwise a reliable full-recording
result is used. Review-required results remain visible as analysis proposals but do not
become normal automatic planning values. If only uncertain results exist, planning has no
reliable automatic BPM; consumers must not silently use the preliminary proposal.

## Persistent diagnostics

Each productive run stores bounded structured diagnostics with its run row: job and run
identity, scope, profile, algorithm, backend, file snapshot, FFprobe and catalog duration,
canonical requested range, every decoded excerpt, raw BPM/alternative/confidence and
correlation score per excerpt, aggregate BPM/confidence/stability, effective parameters,
thresholds, decision reasons and warnings. The track editor's **Diagnosedetails
vergleichen** action shows the latest full and cue runs together and can copy the report.
The per-window `harmonic_quality_score` is deliberately unbounded: it is an
autocorrelation quality score with harmonic and preferred-range bonuses, not a normalized
correlation coefficient. Schema-41 reports written by v0.3 may contain the legacy name
`correlation_score`; the current comparison view maps it to the clarified name.

Pending, running, failed and cancelled runs are explicitly distinguished. The comparison
dialog provides a manual **Aktualisieren** action; it does not poll automatically.

Identical immutable snapshots, algorithm/profile versions, technical parameters and
canonical ranges are processed in fixed range order with deterministic arithmetic. No
temporary audio files or parallel reductions are used. Different encodings can still
decode to slightly different PCM or report slightly different physical durations; the
diagnostics make those inputs and the resulting excerpt positions explicit.

## Known difficult material

Shuffle, syncopation, sparse intros, live timing and genuine section changes remain harder
than constant electronic material. The real reference cases **Toto – Rosanna** and
**Queen – Bohemian Rhapsody** remain documented acceptance references, but their audio is
not stored in the repository. Fade-boundary detection and audible cue preview are separate
features and are not part of the tempo decision.

## Closed findings

The v0.5 acceptance closes the following findings from the earlier prototype and
v0.2–v0.4 field tests:

- long tracks are no longer represented by only three excerpts; five distributed
  excerpts improve temporal coverage;
- only half- and double-tempo relations are merged, eliminating the invalid
  third-pulse merge;
- the existence of a BPM proposal no longer implies automatic plannability;
- aggregate confidence and rhythm stability are independent and diagnosable;
- natural timing spread no longer appears as a blanket 100% stability value;
- stable real drums are no longer blocked by overly conservative raw ACF confidence;
- Rosanna FLAC no longer plans the incorrect family around 112 BPM;
- Bohemian Rhapsody no longer exposes a misleading high-confidence single tempo;
- decoded excerpts, raw window results, effective parameters and aggregation
  contributions are recorded in the comparison diagnostics;
- `harmonic_quality_score` replaces the misleading current name
  `correlation_score`, while the legacy field remains readable;
- pending and running jobs no longer appear as empty completed diagnostics;
- format integration resolves the bundled FFmpeg toolchain as well as PATH, so the
  FLAC, CBR-MP3 and VBR-MP3 tests execute instead of being skipped.

The completed real corpus also closes the format, range and scope investigations:

- there is no evidence of a general FLAC, MP3 or decoding defect;
- controlled FLAC, 320-kbit/s CBR-MP3 and VBR-MP3 derivatives from the same lossless
  source are format-stable under identical analysis conditions;
- small musical start offsets can change weak, competing ACF candidates, but do not
  establish a format defect;
- changing Cue In or Cue Out repositions all distributed analysis windows; it does
  not merely trim the first or last window;
- full-recording and effective-cue analyses are independent, versioned scopes and one
  result does not overwrite the other;
- half- and double-time candidates are correctly assigned to one tempo family;
- third-pulse relations are not accepted as a tempo-family conversion;
- automatic shuffle approval remains a known boundary, safely contained because weak
  or conflicting results cannot become automatic planning values.

All commissioned BPM findings are closed. No production-relevant BPM finding remains
open for `ffmpeg-onset-acf-v0.5` with `tempo-profile-v3`.

## Accepted limitations

These are known domain boundaries, not defects in the accepted implementation:

- Rosanna still requires manual musical judgement when a planning value is needed.
  The half-time shuffle is recognized around 83/166 BPM, but automatic shuffle
  approval is deliberately not guaranteed when competing pulse interpretations keep
  confidence or stability below the safety limits. This is an accepted product
  boundary, not a reason to tune thresholds for this single title.
- A technical aggregate proposal for changing-tempo material is not one uniform song
  tempo and must not be treated as one.
- Automatic cue-boundary detection and BPM estimation solve different problems.
- Visible Cue In and Cue Out values are requested boundaries, not the actual decoded
  sampling excerpts; the diagnostics are authoritative for the latter.
- Audible inspection of the actual internal analysis excerpts is not implemented and
  was outside this feature block. General cue preview is a separate editor feature.
- Fade detection was outside this feature block.
- Manual BPM remains available for difficult or musically ambiguous material.
- `harmonic_quality_score` is deliberately unbounded and is not a normalized
  correlation coefficient.

## Verification record

The implementation close-out used the already completed targeted verification:

- 93 targeted tests passed;
- all three real format-integration tests passed for FLAC, CBR MP3 and VBR MP3, with
  no skips;
- Ruff and the Ruff formatting check passed;
- MyPy passed for the affected production modules;
- `git diff --check` passed.

The complete test suite was deliberately not repeated. It remains part of the later
branch and release completion gate.
