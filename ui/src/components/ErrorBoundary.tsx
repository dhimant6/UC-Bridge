/**
 * Catch render faults and say so.
 *
 * A blank page is the worst possible failure for an operator console: it is
 * indistinguishable from "the server is down" and gives nothing to report. One
 * bad field access in one screen should cost that screen, not the session.
 */

import { Component } from "react";
import type { ErrorInfo, ReactNode } from "react";

interface State {
  error: Error | null;
  stack: string | null;
}

export class ErrorBoundary extends Component<{ children: ReactNode }, State> {
  state: State = { error: null, stack: null };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    this.setState({ stack: info.componentStack ?? null });
    console.error("Screen failed to render", error, info);
  }

  render(): ReactNode {
    const { error, stack } = this.state;
    if (!error) return this.props.children;

    return (
      <div className="callout error">
        <div className="callout-title">This screen failed to render</div>
        <div className="callout-body">
          <p style={{ marginTop: 0 }}>
            {error.name}: {error.message}
          </p>
          <div className="actions">
            <button className="btn" onClick={() => this.setState({ error: null, stack: null })}>
              Try again
            </button>
            <button className="btn" onClick={() => window.location.reload()}>
              Reload the console
            </button>
          </div>
          {stack && (
            <details style={{ marginTop: 10 }}>
              <summary className="faint">Component stack</summary>
              <pre className="code doc">{stack}</pre>
            </details>
          )}
        </div>
      </div>
    );
  }
}
