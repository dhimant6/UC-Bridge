/** The shell: nav, estate selector, role switcher, and the pipeline rail. */

import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import type { Role, StageName } from "../api";
import { Link } from "../router";
import { useApp } from "../state";
import { ApiProblem, Pill } from "./Bits";

/** The nine screens, in the order the work happens. */
export const SCREENS: Array<{
  path: string;
  label: string;
  stage?: StageName;
  group: string;
}> = [
  { path: "/estate", label: "Estate", stage: "discovery", group: "Understand" },
  { path: "/assessment", label: "Assessment", stage: "assessment", group: "Understand" },
  { path: "/mapping", label: "Mapping", stage: "mapping", group: "Prepare" },
  { path: "/waves", label: "Waves", stage: "waves", group: "Prepare" },
  { path: "/plan", label: "Plan & dry run", stage: "dry_run", group: "Prepare" },
  { path: "/runs", label: "Runs", stage: "run", group: "Execute" },
  { path: "/validation", label: "Validation", stage: "validation", group: "Execute" },
  { path: "/audit", label: "Audit", group: "Evidence" },
  { path: "/connectors", label: "Connectors", group: "Evidence" },
];

const RAIL: Array<{ stage: StageName; label: string }> = [
  { stage: "discovery", label: "Discover" },
  { stage: "assessment", label: "Assess" },
  { stage: "mapping", label: "Map" },
  { stage: "waves", label: "Waves" },
  { stage: "plan", label: "Plan" },
  { stage: "dry_run", label: "Dry run" },
  { stage: "run", label: "Apply" },
  { stage: "validation", label: "Validate" },
];

const ROLES: Role[] = ["VIEWER", "PLANNER", "APPROVER", "OPERATOR", "ADMIN"];

function useTheme(): [string, () => void] {
  const [theme, setTheme] = useState(() => localStorage.getItem("ucm.theme") ?? "dark");
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("ucm.theme", theme);
  }, [theme]);
  return [theme, () => setTheme((t) => (t === "dark" ? "light" : "dark"))];
}

export function Layout({ children }: { children: ReactNode }) {
  const { estates, estate, estateId, selectEstate, session, roles, changeRoles, ready, error } =
    useApp();
  const [theme, toggleTheme] = useTheme();

  const stages = estate?.stages;
  const firstIncomplete = RAIL.find((step) => !stages?.[step.stage]);

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-name">UCM-Bridge</div>
          <div className="brand-sub">Migration control plane</div>
        </div>
        <nav className="nav">
          {SCREENS.map((screen, index) => {
            const previous = SCREENS[index - 1];
            const newGroup = !previous || previous.group !== screen.group;
            return (
              <div key={screen.path}>
                {newGroup && <div className="nav-group">{screen.group}</div>}
                <Link
                  to={screen.path}
                  className={(isActive) => `nav-item${isActive ? " active" : ""}`}
                >
                  <span className="nav-step">{index + 1}</span>
                  <span>{screen.label}</span>
                  {screen.stage && (
                    <span
                      className={`nav-dot${stages?.[screen.stage] ? " done" : ""}`}
                      title={stages?.[screen.stage] ? "Complete" : "Not run yet"}
                    />
                  )}
                </Link>
              </div>
            );
          })}
        </nav>
        <div className="sidebar-foot">
          <div>v{session?.version ?? "—"}</div>
          <div style={{ marginTop: 4 }}>
            <a href="/api/docs" target="_blank" rel="noreferrer">
              OpenAPI docs
            </a>
          </div>
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <select
            value={estateId}
            onChange={(event) => selectEstate(event.target.value)}
            aria-label="Estate"
            style={{ maxWidth: 280 }}
          >
            {estates.map((option) => (
              <option key={option.estate_id} value={option.estate_id}>
                {option.name}
              </option>
            ))}
          </select>

          {estate && (
            <div className="rail">
              {RAIL.map((step) => {
                const done = stages?.[step.stage] ?? false;
                const current = firstIncomplete?.stage === step.stage;
                return (
                  <div
                    key={step.stage}
                    className={`rail-step${done ? " done" : ""}${current ? " current" : ""}`}
                    title={done ? "Complete" : "Not run yet"}
                  >
                    <span className="rail-mark">{done ? "✓" : "·"}</span>
                    {step.label}
                  </div>
                );
              })}
            </div>
          )}

          <div className="spacer" style={{ marginLeft: "auto" }} />

          <label className="row" style={{ gap: 6, fontSize: 11 }}>
            <span className="faint">Acting as</span>
            <select
              value={roles[0] ?? "VIEWER"}
              onChange={(event) => changeRoles([event.target.value as Role])}
              aria-label="Role"
            >
              {ROLES.map((role) => (
                <option key={role} value={role}>
                  {role}
                </option>
              ))}
            </select>
          </label>

          <button className="btn tiny" onClick={toggleTheme} title="Toggle theme">
            {theme === "dark" ? "☾" : "☀"}
          </button>
        </header>

        <main className="content">
          {error ? (
            <ApiProblem error={error} />
          ) : !ready ? (
            <div className="empty">
              <span className="spinner" /> Connecting to the control plane…
            </div>
          ) : (
            children
          )}
        </main>
      </div>
    </div>
  );
}

/** Standard page header, with the estate's readiness stated up front. */
export function PageHead({
  title,
  children,
  actions,
}: {
  title: string;
  children?: ReactNode;
  actions?: ReactNode;
}) {
  const { estate } = useApp();
  return (
    <div className="page-head">
      <div className="row">
        <h1>{title}</h1>
        {estate && <Pill kind={estate.target_readiness}>{estate.target_readiness}</Pill>}
        {actions && <span style={{ marginLeft: "auto" }}>{actions}</span>}
      </div>
      {children && <p>{children}</p>}
    </div>
  );
}
