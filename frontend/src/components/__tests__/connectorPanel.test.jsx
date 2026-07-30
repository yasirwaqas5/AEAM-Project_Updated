import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import ConnectorPanel from "../ConnectorPanel";

/* ──────────────────────────────────────────────────────────────────────────
 * Phase F7 — the console half of "connector health is honestly represented".
 *
 * The backend suite proves the report is built from observed state. These prove
 * the renderer does not undo it:
 *
 *  - `stale: null` (never synced) must NOT read as fresh. "We cannot tell" and
 *    "it is current" are different answers and collapsing them is the exact
 *    misrepresentation SEC-8 forbids.
 *  - a never-synced connector must not render as healthy.
 *  - no credential may appear. The payload carries config KEYS and the secret
 *    NAME; a value must never reach the DOM.
 *  - mock mode must be labelled, so fixture data is never mistaken for tenant
 *    data.
 *  - the catalog must show connectors nobody has configured, so an operator can
 *    see what exists.
 * ────────────────────────────────────────────────────────────────────────── */

const CATALOG = [
  {
    kind: "sharepoint", display_name: "SharePoint", capability: "documents",
    required_config: ["drive_id", "site_url"], flag: "CONNECTOR_SHAREPOINT_ENABLED",
    enabled: true,
  },
  {
    kind: "snowflake", display_name: "Snowflake", capability: "metrics",
    required_config: ["account", "database", "query", "warehouse"],
    flag: "CONNECTOR_SNOWFLAKE_ENABLED", enabled: false,
  },
];

function connector(overrides = {}) {
  return {
    source_id: "src-1",
    kind: "sharepoint",
    display_name: "SharePoint",
    name: "Ops Library",
    capability: "documents",
    enabled: true,
    flag: "CONNECTOR_SHAREPOINT_ENABLED",
    configured: true,
    configuration_reason: null,
    authenticated: true,
    error_reason: null,
    sync_status: "succeeded",
    last_successful_sync: "2026-07-30T09:00:00+00:00",
    last_successful_sync_age_seconds: 120,
    last_failed_sync: null,
    stale: false,
    stale_reason: null,
    sync_duration_seconds: 3.5,
    listed_count: 10,
    changed_count: 2,
    processed_count: 2,
    skipped_count: 8,
    failed_count: 0,
    known_artifacts: 10,
    config_keys: ["drive_id", "site_url"],
    secret_ref: "SHAREPOINT_ACCESS_TOKEN",
    ...overrides,
  };
}

function health(overrides = {}) {
  return {
    framework_enabled: true,
    mock_mode: false,
    catalog: CATALOG,
    connectors: [connector()],
    summary: { total: 1, enabled: 1, healthy: 1, unhealthy: 0, unknown: 0 },
    reason: null,
    ...overrides,
  };
}

describe("ConnectorPanel", () => {
  it("says connector state was not queried rather than implying health", () => {
    render(<ConnectorPanel health={null} />);
    expect(screen.getByText(/has not been queried/i)).toBeTruthy();
  });

  it("states the upload + Sheets posture when no connector is enabled", () => {
    render(
      <ConnectorPanel
        health={health({
          framework_enabled: false, connectors: [],
          summary: { total: 0, enabled: 0, healthy: 0, unhealthy: 0, unknown: 0 },
        })}
      />
    );
    expect(screen.getByText(/upload \+ Google Sheets posture/i)).toBeTruthy();
  });

  it("renders a healthy connector with its measured sync counts", () => {
    render(<ConnectorPanel health={health()} />);
    // "SharePoint" appears twice by design: once as the registered connector
    // and once in the catalog of available types.
    expect(screen.getAllByText("SharePoint")).toHaveLength(2);
    expect(screen.getByText("healthy")).toBeTruthy();
    expect(screen.getByText("processed=2")).toBeTruthy();
    // The skipped count is the visible evidence that incremental sync worked.
    expect(screen.getByText("skipped=8")).toBeTruthy();
  });

  it("renders a never-synced connector as unknown, never as fresh or healthy", () => {
    render(
      <ConnectorPanel
        health={health({
          connectors: [connector({
            sync_status: "never_synced",
            last_successful_sync: null,
            last_successful_sync_age_seconds: null,
            stale: null,
            stale_reason: "This connector has never completed a sync, so staleness is not computable.",
          })],
        })}
      />
    );
    expect(screen.getByText("never synced")).toBeTruthy();
    expect(screen.getByText(/stale: not computable/i)).toBeTruthy();
    expect(screen.getByText(/never completed a sync/i)).toBeTruthy();
    expect(screen.queryByText("healthy")).toBeNull();
    expect(screen.queryByText(/stale: no$/)).toBeNull();
  });

  it("surfaces a configuration gap with the missing keys named", () => {
    render(
      <ConnectorPanel
        health={health({
          connectors: [connector({
            configured: false,
            configuration_reason: "Missing required configuration: drive_id.",
            authenticated: false,
            config_keys: ["site_url"],
          })],
        })}
      />
    );
    expect(screen.getByText("not configured")).toBeTruthy();
    expect(screen.getByText(/Missing required configuration: drive_id/)).toBeTruthy();
  });

  it("surfaces an authentication failure as unhealthy with its reason", () => {
    render(
      <ConnectorPanel
        health={health({
          connectors: [connector({
            authenticated: false,
            error_reason: "No credential available. Set 'SHAREPOINT_ACCESS_TOKEN'.",
          })],
        })}
      />
    );
    expect(screen.getByText("not authenticated")).toBeTruthy();
    expect(screen.getByText(/No credential available/)).toBeTruthy();
  });

  it("renders the secret NAME and config KEYS but never a credential value", () => {
    const { container } = render(<ConnectorPanel health={health()} />);
    expect(screen.getByText(/secret_ref: SHAREPOINT_ACCESS_TOKEN/)).toBeTruthy();
    expect(screen.getByText(/config: \[drive_id, site_url\]/)).toBeTruthy();
    // Nothing token-shaped anywhere in the rendered output.
    expect(container.textContent).not.toMatch(/eyJ|ghp_|Bearer\s+\S+/);
  });

  it("labels mock mode so fixture data is never mistaken for tenant data", () => {
    render(<ConnectorPanel health={health({ mock_mode: true })} />);
    expect(screen.getByText("MOCK MODE")).toBeTruthy();
    expect(screen.getByText(/deterministic in-repo mock clients/i)).toBeTruthy();
  });

  it("shows the catalog including connectors nobody has configured", () => {
    render(<ConnectorPanel health={health()} />);
    expect(screen.getByText(/Available connector types \(2\)/)).toBeTruthy();
    expect(screen.getByText("Snowflake")).toBeTruthy();
    expect(screen.getByText("disabled")).toBeTruthy();
  });

  it("keeps unknown as its own summary bucket", () => {
    render(
      <ConnectorPanel
        health={health({ summary: { total: 3, enabled: 2, healthy: 1, unhealthy: 1, unknown: 1 } })}
      />
    );
    expect(screen.getByText("1 unknown")).toBeTruthy();
    expect(screen.getByText("1 healthy")).toBeTruthy();
    expect(screen.getByText("1 unhealthy")).toBeTruthy();
  });

  it("offers a sync trigger for an enabled connector and withholds it otherwise", () => {
    const onSync = vi.fn();
    render(
      <ConnectorPanel
        health={health({
          connectors: [connector(), connector({ source_id: "src-2", enabled: false })],
        })}
        onSync={onSync}
      />
    );
    const buttons = screen.getAllByRole("button", { name: /sync/i });
    expect(buttons).toHaveLength(2);
    expect(buttons[0].disabled).toBe(false);
    expect(buttons[1].disabled).toBe(true);
  });

  it("tells the operator what to do when no connector source is registered", () => {
    render(
      <ConnectorPanel
        health={health({
          connectors: [],
          summary: { total: 0, enabled: 0, healthy: 0, unhealthy: 0, unknown: 0 },
        })}
      />
    );
    expect(screen.getByText(/No connector sources are registered/i)).toBeTruthy();
  });

  it("states that connector content shares the upload ingestion path", () => {
    render(<ConnectorPanel health={health()} />);
    expect(screen.getByText(/same ingestion pipeline as an uploaded file/i)).toBeTruthy();
  });
});
