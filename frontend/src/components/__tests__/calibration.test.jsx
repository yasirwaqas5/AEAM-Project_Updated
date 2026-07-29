import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { getCalibration, CalibrationNote } from "../ui";

/* ──────────────────────────────────────────────────────────────────────────
 * Phase F2 — the console half of "both raw and calibrated confidence are
 * persisted and shown".
 *
 * The backend suite proves both values are persisted. These prove they are
 * shown, and — just as importantly — that a calibrated number never appears
 * without the raw value it was adjusted from. A confidence that silently
 * moved is worse than an uncalibrated one, because nobody can tell it moved
 * (EXPL-4: adjustments are disclosed with their magnitude and their reason).
 *
 * Also covers the COMPAT-1 case that matters most here: an incident recorded
 * before F2 carries no calibration key at all and must render exactly as it
 * always did.
 * ────────────────────────────────────────────────────────────────────────── */

function incidentWith(calibration) {
  return { findings: [{ type: "audit_summary", calibration }] };
}

describe("getCalibration", () => {
  it("returns null for an incident recorded before F2", () => {
    expect(getCalibration({ findings: [{ type: "audit_summary" }] })).toBeNull();
  });

  it("returns null for an incident with no findings at all", () => {
    expect(getCalibration({})).toBeNull();
    expect(getCalibration(null)).toBeNull();
  });

  it("ignores a malformed calibration rather than rendering a half-truth", () => {
    // `applied` is the field every consumer branches on; without a boolean
    // there is no honest way to describe the number, so the disclosure is
    // withheld entirely rather than guessed at.
    expect(getCalibration(incidentWith({ confidence_raw: 0.9 }))).toBeNull();
    expect(getCalibration(incidentWith({ applied: "yes" }))).toBeNull();
  });

  it("reads an applied calibration with its raw value and magnitude", () => {
    const calibration = getCalibration(
      incidentWith({
        applied: true,
        confidence_raw: 0.9,
        confidence_calibrated: 0.55,
        adjustment: -0.35,
        calibration_version: 3,
        reason: "Calibration v3, fitted on 266 labeled outcomes.",
      })
    );

    expect(calibration.applied).toBe(true);
    expect(calibration.raw).toBe(0.9);
    expect(calibration.calibrated).toBe(0.55);
    expect(calibration.adjustment).toBe(-0.35);
    expect(calibration.version).toBe(3);
    expect(calibration.reason).toContain("266");
  });

  it("reads a not-applied calibration together with its reason", () => {
    const calibration = getCalibration(
      incidentWith({
        applied: false,
        reason: "Confidence recalibration is disabled for this deployment.",
      })
    );

    expect(calibration.applied).toBe(false);
    expect(calibration.reason).toContain("disabled");
  });
});

describe("CalibrationNote", () => {
  it("renders nothing for a pre-F2 incident", () => {
    const { container } = render(<CalibrationNote calibration={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("says the confidence is raw when calibration was not applied", () => {
    render(
      <CalibrationNote
        calibration={{
          applied: false,
          reason: "Confidence recalibration is disabled for this deployment.",
        }}
      />
    );

    expect(screen.getByText(/Raw confidence/i)).toBeInTheDocument();
    // The reason must be reachable, not swallowed: an operator setting an
    // automation threshold needs to know WHY the number is uncalibrated.
    expect(screen.getByTitle(/disabled for this deployment/i)).toBeInTheDocument();
  });

  it("shows the raw value and the adjustment whenever a number was moved", () => {
    render(
      <CalibrationNote
        calibration={{
          applied: true,
          raw: 0.9,
          calibrated: 0.55,
          adjustment: -0.35,
          version: 3,
          reason: "Calibration v3, fitted on 266 labeled outcomes.",
        }}
      />
    );

    expect(screen.getByText(/Calibrated/)).toBeInTheDocument();
    expect(screen.getByText(/v3/)).toBeInTheDocument();
    // The raw value the number came from — the whole point of the note.
    expect(screen.getByText("90%")).toBeInTheDocument();
    expect(screen.getByText(/35 pts/)).toBeInTheDocument();
  });

  it("marks a downward adjustment differently from an upward one", () => {
    const { unmount } = render(
      <CalibrationNote calibration={{ applied: true, raw: 0.9, adjustment: -0.35, version: 1 }} />
    );
    expect(screen.getByText(/▼/)).toBeInTheDocument();
    unmount();

    render(
      <CalibrationNote calibration={{ applied: true, raw: 0.1, adjustment: 0.05, version: 1 }} />
    );
    expect(screen.getByText(/▲/)).toBeInTheDocument();
  });

  it("renders without a version or adjustment rather than inventing them", () => {
    render(<CalibrationNote calibration={{ applied: true, raw: 0.4 }} />);

    expect(screen.getByText(/Calibrated/)).toBeInTheDocument();
    expect(screen.queryByText(/pts/)).not.toBeInTheDocument();
    expect(screen.queryByText(/v(null|undefined)/)).not.toBeInTheDocument();
  });
});
