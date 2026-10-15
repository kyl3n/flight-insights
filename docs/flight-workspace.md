# Flights workspace

Flight Lab centers the normal workflow on **Setup** and **Flights**. Flights
contains collection controls, original-log import, chronological history and
the selected flight's analysis. Manual analysis, experiment design, research and
evidence-management tools remain available under **Advanced tools**.

## Build the flight history

Import original DataFlash BIN/LOG or timestamped MAVLink TLOG files from Flights.
The app preserves originals and automatically indexes supported sources, clock
segments and observed arm/disarm intervals. It then generates local diagnostic
reports in bounded, cancellable background work. Completed app telemetry captures
can enter this workflow after their originals have been archived.

A log file is not automatically one flight. Multiple observed armed intervals
remain separate. Missing boundaries produce an interval or log-segment review
item; an armed bench test does not establish airborne flight. Reimporting the
same original does not add another independent flight to the library.

Known dates use supported reported UTC or explicitly labelled TLOG receipt time.
Files with unknown flight dates are grouped separately; file names, filesystem
timestamps and import dates never establish chronological flight order. The
recent-history window is the 30 days preceding the latest dated flight, so an
older imported month can be reviewed as a history without relabelling it current.

Automatic library analysis pauses while live collection owns the app connection,
leaving the background worker available for parameter capture. Retained originals
and completed reports remain available. A cancelled or interrupted analysis can
be resumed from the library; no partial result is labelled complete.

## Open a flight

Selecting an entry brings its measured metrics, findings, configuration evidence,
software context and conditional improvement advice into that flight's detail.
The app prepares comparisons in background work and automatically chooses the preceding compatible flight when identity,
ordering and interval evidence support the choice. Recent compatible history
appears alongside that comparison.

Automatic grouping uses an observed hardware UID or an explicit aircraft label
declared by the user. Matching MAVLink system numbers or filenames are insufficient.
Declared grouping remains labelled as declared; it is not authenticated aircraft
identity. Different synthetic provenance, incompatible clock semantics, uncertain
boundaries or overlapping intervals prevent automatic pairing. Unknown dates can
support ordering only between separated intervals in the same original source and
clock segment.

**Flight context** supplies missing aircraft grouping, battery-monitor selection
for a selected analysis. Additional operating context is available through manual analysis. Multiple battery monitors are never
summed or silently selected. Unknown fields stay unknown. For a special phase,
sensor, spectral window or filter model, use the manual analysis tools.

Metric deltas are descriptive observations. The detail includes configuration and
environment changes, coverage and other conditions that can affect interpretation.
It does not assert that a changed parameter caused a measured change or that a
configuration is optimal. Conditional recommendations retain the existing cited
mechanisms, firmware/hardware prerequisites, alternatives and evidence gaps.

## Keep historical context attached to its evidence

Collection preserves the available host OS, Flight Lab and recognized application
versions at capture and arm transitions. A historical imported log does not gain
the current host's software context. The app can display its own archived telemetry
with the recorded environment
through exact capture UUID, original hash and arm/disarm record-span membership.
This is labelled **capture provenance** and does not verify another imported log
or a complete configuration.

After collection stops, background analysis can evaluate independent imported
logs against the app's completed capture. It uses the existing alignment rule:
matching reported UID/firmware, supported clocks, exact observed arm/disarm spans
and a complete bounded candidate universe. Only freshly verified matches are
attached automatically; ambiguous, rejected or incomplete candidates remain for
review. The app's own capture alone cannot establish this independent match.
Alignment runs in batches of at most two attempts per background pass, with
pending work continued by the workspace. Unchanged evidence does not repeat the
same alignment work on every refresh.

Independently verified flight context and complete preflight readbacks contribute
to reports only when their exact flight association passes
the existing content, identity, clock and original-integrity checks. A shared app
connection simplifies collection; it does not relax those association rules.

Logged parameter observations remain distinct from complete typed readbacks and
effective settings. A postflight or current snapshot cannot fill a missing
historical preflight snapshot. Unsupported configuration, logging or identity
evidence is shown as missing or needing review.

Original files and saved reports remain local and immutable. Library rows are a
derived navigation index, not replacement source evidence. Advanced collection
details retain manifests, interrupted captures, acquisition outcomes and
association review. Refer to [performance insights](performance-insights.md),
[managed collection](managed-collection.md) and [evidence alignment](evidence-alignment.md)
for the detailed analysis and provenance contracts. Large combined details are
bounded: an explicit notice identifies a comparison or report omitted from the
combined view; the saved evidence remains accessible in advanced report tools.
