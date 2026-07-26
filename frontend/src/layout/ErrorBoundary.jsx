import { Component } from "react";
import { Card, Button, Icon } from "../components/ui";

/* ──────────────────────────────────────────────────────────────────────────
 * layout/ErrorBoundary.jsx
 *
 * Phase E10: a render error in one page must not blank the entire console.
 * Class component because React error boundaries have no hook equivalent.
 * No business logic -- catches, logs, and offers a reload.
 * ────────────────────────────────────────────────────────────────────────── */

export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    // eslint-disable-next-line no-console
    console.error("AEAM console render error:", error, info);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div style={{ padding: "var(--sp-8)", display: "flex", justifyContent: "center" }}>
        <Card style={{ maxWidth: 480, textAlign: "center", padding: "var(--sp-6)" }}>
          <Icon name="alert" size={28} color="var(--err)" />
          <h2 style={{ marginTop: "var(--sp-3)", fontSize: "var(--fs-lg)", color: "var(--text)" }}>
            Something went wrong rendering this page
          </h2>
          <p style={{ color: "var(--muted)", fontSize: "var(--fs-sm)", marginTop: "var(--sp-2)" }}>
            {this.state.error?.message || "An unexpected error occurred."}
          </p>
          <div style={{ marginTop: "var(--sp-4)" }}>
            <Button variant="primary" onClick={() => window.location.reload()}>Reload console</Button>
          </div>
        </Card>
      </div>
    );
  }
}
