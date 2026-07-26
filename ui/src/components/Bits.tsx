/** Small shared pieces. Nothing here knows about the domain. */

import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { ApiError } from "../api";

export function Panel({
  title,
  note,
  actions,
  flush,
  children,
}: {
  title?: ReactNode;
  note?: ReactNode;
  actions?: ReactNode;
  flush?: boolean;
  children: ReactNode;
}) {
  return (
    <section className="panel">
      {(title || actions) && (
        <header className="panel-head">
          {title && <h2>{title}</h2>}
          {note && <span className="panel-note">{note}</span>}
          {actions && <span className="spacer">{actions}</span>}
        </header>
      )}
      <div className={flush ? "panel-body flush" : "panel-body"}>{children}</div>
    </section>
  );
}

export function Pill({ kind, children }: { kind: string; children?: ReactNode }) {
  return <span className={`pill ${kind}`}>{children ?? kind}</span>;
}

export function Stat({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: "good" | "warn" | "bad";
}) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className={tone ? `stat-value ${tone}` : "stat-value"}>{value}</div>
      {hint && <div className="stat-hint">{hint}</div>}
    </div>
  );
}

export function Callout({
  tone = "info",
  title,
  children,
}: {
  tone?: "info" | "ok" | "refused" | "error";
  title: ReactNode;
  children?: ReactNode;
}) {
  return (
    <div className={`callout ${tone}`}>
      <div className="callout-title">{title}</div>
      {children && <div className="callout-body">{children}</div>}
    </div>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <div className="empty-title">{title}</div>
      {children && <div>{children}</div>}
    </div>
  );
}

/**
 * Render an API failure.
 *
 * A refusal and a fault look different on purpose. A 422 from a guardrail means
 * the product did its job, and presenting that as a red error teaches operators
 * to ignore red errors.
 */
export function ApiProblem({ error }: { error: unknown }) {
  if (!(error instanceof ApiError)) {
    return (
      <Callout tone="error" title="Request failed">
        {error instanceof Error ? error.message : String(error)}
      </Callout>
    );
  }
  if (error.status === 409) {
    return (
      <Callout tone="info" title={`Not ready yet — ${error.kind}`}>
        {error.message}
      </Callout>
    );
  }
  if (error.status === 403) {
    return (
      <Callout tone="refused" title="Refused: your role does not carry this permission">
        {error.message}
      </Callout>
    );
  }
  if (error.guardrail) {
    return (
      <Callout tone="refused" title={`Refused by a guardrail — ${error.kind}`}>
        {error.message}
        <div className="faint" style={{ marginTop: 6 }}>
          This is the platform working as designed. The refusal is enforced in the library, not in
          the API, so it cannot be bypassed by calling a different endpoint.
        </div>
      </Callout>
    );
  }
  return (
    <Callout tone="error" title={`${error.kind} (HTTP ${error.status})`}>
      {error.message}
    </Callout>
  );
}

export interface AsyncState<T> {
  data: T | undefined;
  error: unknown;
  loading: boolean;
  reload: () => void;
  set: (value: T) => void;
}

/** Load once per changing key, and expose a manual reload. */
export function useAsync<T>(load: () => Promise<T>, deps: unknown[]): AsyncState<T> {
  const [data, setData] = useState<T | undefined>(undefined);
  const [error, setError] = useState<unknown>(undefined);
  const [loading, setLoading] = useState(true);
  const [nonce, setNonce] = useState(0);
  const loadRef = useRef(load);
  loadRef.current = load;

  useEffect(() => {
    let live = true;
    setLoading(true);
    setError(undefined);
    loadRef
      .current()
      .then((value) => {
        if (live) setData(value);
      })
      .catch((cause) => {
        if (live) {
          setData(undefined);
          setError(cause);
        }
      })
      .finally(() => {
        if (live) setLoading(false);
      });
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  return { data, error, loading, reload, set: setData };
}

/** A button that runs an async action and surfaces whatever came back. */
export function ActionButton({
  label,
  run,
  onDone,
  disabled,
  reason,
  variant = "primary",
}: {
  label: string;
  run: () => Promise<unknown>;
  onDone?: (result: unknown) => void;
  disabled?: boolean;
  reason?: string;
  variant?: "primary" | "plain" | "danger";
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(undefined);

  const click = async () => {
    setBusy(true);
    setError(undefined);
    try {
      const result = await run();
      onDone?.(result);
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  };

  const className = `btn${variant === "plain" ? "" : ` ${variant}`}`;
  return (
    <>
      <button
        className={className}
        onClick={click}
        disabled={busy || disabled}
        title={disabled ? reason : undefined}
      >
        {busy ? <span className="spinner" /> : null} {label}
      </button>
      {error ? (
        <div style={{ width: "100%", marginTop: 8 }}>
          <ApiProblem error={error} />
        </div>
      ) : null}
    </>
  );
}

export function Json({ value }: { value: unknown }) {
  return <pre className="code">{JSON.stringify(value, null, 2)}</pre>;
}

/** Markdown as-is. The library already renders it for the CAB pack. */
export function Document({ text }: { text: string }) {
  return <pre className="code doc">{text}</pre>;
}

export function Bar({ value, tone }: { value: number; tone?: "good" | "bad" }) {
  const pct = Math.max(0, Math.min(1, value)) * 100;
  return (
    <div className="bar">
      <span className={tone} style={{ width: `${pct}%` }} />
    </div>
  );
}
