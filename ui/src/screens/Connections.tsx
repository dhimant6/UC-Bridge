/**
 * Screen 10 — Connections.
 *
 * Where demo stops and a real system starts. Two questions live here and they
 * are deliberately kept apart, because conflating them is how someone ends up
 * writing to a production PBX with a connector nobody verified:
 *
 * **Can this connection be opened?** Does a transport exist and is it
 * configured. A client-side question.
 *
 * **May this connector write to production?** The readiness gate. Answered
 * identically in demo and live, and a connection can be perfectly openable while
 * still refused every write.
 */

import { api } from "../api";
import type { ConnectionEntry } from "../api";
import { PageHead } from "../components/Layout";
import {
  ActionButton,
  ApiProblem,
  Callout,
  Empty,
  Json,
  Panel,
  Pill,
  Stat,
  useAsync,
} from "../components/Bits";
import { useApp, useGate } from "../state";

export function ConnectionsScreen() {
  const { mode } = useApp();
  const gate = useGate("MANAGE_CONNECTORS");
  const view = useAsync(() => api.connections(), []);

  if (view.error) return <ApiProblem error={view.error} />;
  if (!view.data) return <Empty title="Loading connections…" />;

  const { connections, transport_support } = view.data;
  const openable = connections.filter((c) => c.can_open);

  return (
    <>
      <PageHead title="Connections">
        A connection profile is the address of a real system plus a reference to its credential —
        never the credential itself, which is resolved through the broker at call time. Profiles
        are safe to commit and safe to put on this screen.
      </PageHead>

      {mode?.is_live ? (
        <Callout tone="refused" title="This process is in LIVE mode">
          Connectors here build real transports against real systems. The readiness gate is
          unchanged by that — it still decides what may be written, and it is evaluated exactly as
          it is in demo.
        </Callout>
      ) : (
        <Callout tone="info" title="This process is in DEMO mode">
          Every connector is driven by recorded cassettes. Nothing reaches a network and no
          credential is held. Set <span className="mono">UCM_BRIDGE_MODE=LIVE</span> and supply a
          connection file to change that — demo is the default, and an unrecognised value stays
          demo rather than erroring, because a typo must never be why something dials a PBX.
        </Callout>
      )}

      <Panel title="This deployment">
        <div className="stats" style={{ borderRadius: 4 }}>
          <Stat label="Mode" value={mode?.mode ?? "—"} tone={mode?.is_live ? "warn" : "good"} />
          <Stat label="Connections" value={connections.length} />
          <Stat label="Live" value={connections.filter((c) => c.is_live).length} />
          <Stat
            label="Openable"
            value={openable.length}
            tone={openable.length > 0 ? "good" : undefined}
            hint="transport exists"
          />
          <Stat
            label="Persistence"
            value={mode?.persistence ? "on disk" : "in process"}
            tone={mode?.persistence ? "good" : "warn"}
            hint={mode?.persistence ?? "a restart loses the audit chain"}
          />
        </div>
      </Panel>

      {connections.length === 0 ? (
        <Panel title="No connections configured">
          <Empty title="Nothing to connect to yet">
            Point <span className="mono">UCM_BRIDGE_CONNECTIONS</span> at a JSON file holding a
            registry of profiles. An absent file is not an error — it is what makes an
            unconfigured process a demo process.
          </Empty>
        </Panel>
      ) : (
        connections.map((entry) => (
          <ConnectionCard
            key={entry.profile.connection_id}
            entry={entry}
            canTest={gate.allowed}
            testReason={gate.reason}
          />
        ))
      )}

      <Panel
        title="What each connector needs before it can be pointed at a real system"
        note="Derived from the factory, so it cannot promise what the factory would refuse"
        flush
      >
        <table>
          <thead>
            <tr>
              <th>Connector</th>
              <th>Transports</th>
              <th>Can connect</th>
            </tr>
          </thead>
          <tbody>
            {transport_support.map((row) => (
              <tr key={row.connector_id}>
                <td className="mono">{row.connector_id}</td>
                <td>
                  <span className="row" style={{ gap: 5 }}>
                    {row.transports.map((t) => (
                      <Pill key={t.kind} kind={t.implemented ? "good" : "warn"}>
                        {t.kind}
                        {t.implemented ? "" : " — not built"}
                      </Pill>
                    ))}
                  </span>
                </td>
                <td>
                  {row.ready_to_connect ? (
                    <Pill kind="good">yes</Pill>
                  ) : (
                    <Pill kind="warn">blocked</Pill>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>
    </>
  );
}

function ConnectionCard({
  entry,
  canTest,
  testReason,
}: {
  entry: ConnectionEntry;
  canTest: boolean;
  testReason: string;
}) {
  const { profile, readiness } = entry;

  return (
    <Panel
      title={profile.display_name}
      note={
        <span className="row" style={{ gap: 6 }}>
          <Pill kind={entry.is_live ? "warn" : "muted"}>{profile.mode}</Pill>
          <Pill kind="plain">{entry.transport}</Pill>
          {readiness && <Pill kind={readiness.level}>{readiness.level}</Pill>}
        </span>
      }
      actions={
        <ActionButton
          label="Preflight"
          variant="plain"
          run={() => api.testConnection(profile.connection_id)}
          disabled={!canTest || !entry.can_open}
          reason={
            !entry.can_open
              ? (entry.blocked_reason ?? "A demo profile has nothing to reach.")
              : testReason
          }
          onDone={(result) => console.info("preflight", result)}
        />
      }
    >
      <div className="stack">
        <dl className="kv">
          <dt>Connection id</dt>
          <dd className="mono">{profile.connection_id}</dd>
          <dt>Connector</dt>
          <dd className="mono">{profile.connector_id}</dd>
          <dt>Instance</dt>
          <dd className="mono">{profile.instance_id}</dd>
          {profile.endpoint && (
            <>
              <dt>Address</dt>
              <dd className="mono wrap-anywhere">{profile.endpoint.address ?? "platform default"}</dd>
              <dt>Verify TLS</dt>
              <dd>
                {profile.endpoint.verify_tls ? (
                  "yes"
                ) : (
                  <Pill kind="bad">disabled — recorded on the profile</Pill>
                )}
              </dd>
            </>
          )}
          {profile.credential && (
            <>
              <dt>Credential</dt>
              <dd className="mono">
                {profile.credential.provider}:{profile.credential.path}
              </dd>
              <dt>Scope</dt>
              <dd>
                <Pill kind={profile.credential.scope === "READ_ONLY" ? "good" : "warn"}>
                  {profile.credential.scope}
                </Pill>
              </dd>
            </>
          )}
        </dl>

        {profile.notes && <div className="muted">{profile.notes}</div>}

        {entry.blocked_reason && (
          <Callout tone="refused" title="This connection cannot be opened yet">
            {entry.blocked_reason}
          </Callout>
        )}

        {readiness && !readiness.level.startsWith("PRODUCTION") && (
          <Callout tone="info" title={`Readiness: ${readiness.level}`}>
            A separate question from whether the connection opens. This connector may extract but
            not write to production until it clears the gate.
            {readiness.notes.map((note) => (
              <div key={note} style={{ marginTop: 4 }}>
                {note}
              </div>
            ))}
          </Callout>
        )}

        {profile.endpoint && Object.keys(profile.endpoint.options).length > 0 && (
          <div>
            <h3 style={{ marginBottom: 6 }}>Transport options</h3>
            <Json value={profile.endpoint.options} />
          </div>
        )}
      </div>
    </Panel>
  );
}
