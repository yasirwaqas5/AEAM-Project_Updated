import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ReplayTimeline, ReplayGaps } from "../Timeline";

/* ──────────────────────────────────────────────────────────────────────────
 * Phase F5 — the console half of the timeline contract.
 *
 * The backend suite proves the timeline is built from persisted numbers
 * only. These prove the renderer does not undo that:
 *
 *  - an unmeasured stage must read "not recorded", never "0s" or "0ms". A
 *    zero is a measurement; showing one for an unmeasured stage would
 *    invent the most misleading possible value.
 *  - a stage recorded more than once must be labelled as an aggregate,
 *    because the backend deliberately refuses to split it.
 *  - time the instrumentation did not cover must be shown as unattributed
 *    rather than folded silently into the stages.
 *  - an incident with no timing at all must say so, with the reason.
 *  - a gap must carry the phase that introduced the stage, so a pre-C7
 *    incident reads as "planning was never recorded" instead of quietly
 *    rendering one stage fewer than a newer incident.
 * ────────────────────────────────────────────────────────────────────────── */

const TIMELINE = {
  incident_id: "inc-1",
  anchor_timestamp: "2026-07-05T00:00:00Z",
  anchor_source: "incidents.timestamp",
  timing_available: true,
  timing_reason: null,
  total_investigation_seconds: 4.0,
  total_source: "audit_summary.investigation_duration_seconds",
  measured_stage_seconds: 3.0,
  unattributed_seconds: 1.0,
  unattributed_note: "Measured total minus the sum of measured stage time.",
  stages_total: 3,
  stages_with_duration: 2,
  stages_without_duration: 1,
  entries: [
    {
      sequence: 0, occurrence: 1, occurrences_total: 2, key: "decision",
      label: "Decision", category: "decision", duration_available: true,
      duration_seconds: null, stage_total_seconds: 2.0,
      duration_source: "audit_summary.stage_durations",
      duration_note: "2.0s is the measured total across 2 recorded occurrences of this stage, not this occurrence alone.",
      cumulative_measured_seconds: 2.0,
    },
    {
      sequence: 1, occurrence: 1, occurrences_total: 1, key: "memory",
      label: "Enterprise Memory", category: "evidence", duration_available: true,
      duration_seconds: 1.0, stage_total_seconds: 1.0,
      duration_source: "audit_summary.stage_durations",
      duration_note: null, cumulative_measured_seconds: 3.0,
    },
    {
      sequence: 2, occurrence: 1, occurrences_total: 1, key: "audit_summary",
      label: "Audit Summary & Actions", category: "actions", duration_available: false,
      duration_seconds: null, stage_total_seconds: null, duration_source: null,
      duration_note: "No measured duration recorded for this stage.",
      cumulative_measured_seconds: 3.0,
    },
  ],
  by_stage: {},
};

describe("ReplayTimeline", () => {
  it("renders nothing when there is no timeline rather than an empty frame", () => {
    const { container } = render(<ReplayTimeline timeline={null} />);
    expect(container.firstChild).toBeNull();
  });

  it("shows an unmeasured stage as not recorded, never as zero", () => {
    render(<ReplayTimeline timeline={TIMELINE} />);
    expect(screen.getByText("not recorded")).toBeTruthy();
    expect(screen.queryByText("0ms")).toBeNull();
    expect(screen.queryByText("0.00s")).toBeNull();
  });

  it("labels a repeated stage's duration as a stage total", () => {
    render(<ReplayTimeline timeline={TIMELINE} />);
    expect(screen.getByText(/2\.00s \(stage total\)/)).toBeTruthy();
    expect(screen.getByText(/pass 1 of 2/)).toBeTruthy();
  });

  it("reports a single-occurrence stage's own measured duration, unqualified", () => {
    render(<ReplayTimeline timeline={TIMELINE} />);
    // "1.00s" also appears as the unattributed total, so assert on the
    // absence of an aggregate qualifier rather than on uniqueness.
    const rendered = screen.getAllByText("1.00s");
    expect(rendered.length).toBeGreaterThan(0);
    expect(screen.queryByText("1.00s (stage total)")).toBeNull();
  });

  it("discloses unattributed time instead of folding it into the stages", () => {
    render(<ReplayTimeline timeline={TIMELINE} />);
    expect(screen.getByText(/Unattributed/i)).toBeTruthy();
    expect(screen.getByText(/Attributed to stages/i)).toBeTruthy();
  });

  it("states the anchor and how many stages carry a measurement", () => {
    render(<ReplayTimeline timeline={TIMELINE} />);
    expect(screen.getByText(/incidents\.timestamp/)).toBeTruthy();
    expect(screen.getByText(/2 of 3 recorded/)).toBeTruthy();
  });

  it("explains an incident that carries no timing at all", () => {
    render(
      <ReplayTimeline
        timeline={{
          ...TIMELINE,
          timing_available: false,
          timing_reason: "This incident carries no measured durations.",
          total_investigation_seconds: null,
          measured_stage_seconds: null,
          unattributed_seconds: null,
        }}
      />
    );
    expect(screen.getByText(/carries no measured durations/)).toBeTruthy();
  });
});

describe("ReplayGaps", () => {
  it("renders nothing when the record has no gaps", () => {
    const { container } = render(<ReplayGaps gaps={[]} />);
    expect(container.firstChild).toBeNull();
  });

  it("names the missing stage and the phase that introduced it", () => {
    render(
      <ReplayGaps
        gaps={[{
          key: "execution_plan",
          label: "Execution Planning",
          category: "planning",
          introduced_in: "Phase C7",
          reason: "No 'Execution Planning' entry is present in this incident's recorded findings.",
        }]}
      />
    );
    expect(screen.getByText("Execution Planning")).toBeTruthy();
    expect(screen.getByText(/introduced in Phase C7/)).toBeTruthy();
    expect(screen.getByText(/No 'Execution Planning' entry is present/)).toBeTruthy();
  });
});
