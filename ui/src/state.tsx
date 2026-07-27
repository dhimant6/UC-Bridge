/**
 * App-wide state: which estate is selected, who we are acting as, and the
 * pipeline progress every screen reads to decide what it can offer.
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { api, getRoles, setRoles } from "./api";
import type { EstateState, ModeInfo, Role, SessionInfo } from "./api";

interface AppValue {
  /** DEMO or LIVE. Fetched unauthenticated so the banner can precede login. */
  mode: ModeInfo | undefined;
  estates: EstateState[];
  estate: EstateState | undefined;
  estateId: string;
  selectEstate: (id: string) => void;
  session: SessionInfo | undefined;
  roles: Role[];
  changeRoles: (roles: Role[]) => void;
  can: (permission: string) => boolean;
  /** Which roles grant a permission — so a disabled button can say why. */
  grantedBy: (permission: string) => Role[];
  refresh: () => void;
  ready: boolean;
  error: unknown;
}

const AppContext = createContext<AppValue | null>(null);

const ROLE_KEY = "ucm.roles";
const ESTATE_KEY = "ucm.estate";

function initialRoles(): Role[] {
  const stored = localStorage.getItem(ROLE_KEY);
  if (!stored) return ["PLANNER"];
  const parsed = stored.split(",").filter(Boolean) as Role[];
  return parsed.length > 0 ? parsed : ["PLANNER"];
}

export function AppProvider({ children }: { children: ReactNode }) {
  const [roles, setRoleState] = useState<Role[]>(() => {
    const initial = initialRoles();
    setRoles(initial);
    return initial;
  });
  const [estates, setEstates] = useState<EstateState[]>([]);
  const [mode, setMode] = useState<ModeInfo | undefined>(undefined);
  const [session, setSession] = useState<SessionInfo | undefined>(undefined);
  const [estateId, setEstateId] = useState<string>(() => localStorage.getItem(ESTATE_KEY) ?? "");
  const [nonce, setNonce] = useState(0);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<unknown>(undefined);

  const refresh = useCallback(() => setNonce((n) => n + 1), []);

  // The mode is fetched separately and never blocks: it is unauthenticated on
  // purpose, so a live banner can go up even when the rest 401s.
  useEffect(() => {
    let alive = true;
    api
      .mode()
      .then((info) => {
        if (alive) setMode(info);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [nonce]);

  useEffect(() => {
    let live = true;
    Promise.all([api.session(), api.estates()])
      .then(([sessionInfo, list]) => {
        if (!live) return;
        setSession(sessionInfo);
        setEstates(list);
        setEstateId((current) => {
          if (current && list.some((e) => e.estate_id === current)) return current;
          return list[0]?.estate_id ?? "";
        });
        setError(undefined);
      })
      .catch((cause) => {
        if (live) setError(cause);
      })
      .finally(() => {
        if (live) setReady(true);
      });
    return () => {
      live = false;
    };
  }, [nonce, roles]);

  const selectEstate = useCallback((id: string) => {
    setEstateId(id);
    localStorage.setItem(ESTATE_KEY, id);
  }, []);

  const changeRoles = useCallback((next: Role[]) => {
    const value = next.length > 0 ? next : (["VIEWER"] as Role[]);
    setRoles(value);
    setRoleState(value);
    localStorage.setItem(ROLE_KEY, value.join(","));
  }, []);

  const value = useMemo<AppValue>(() => {
    const permissions = new Set(session?.permissions ?? []);
    const catalogue = session?.role_catalogue ?? ({} as Record<Role, string[]>);
    return {
      mode,
      estates,
      estate: estates.find((e) => e.estate_id === estateId),
      estateId,
      selectEstate,
      session,
      roles: getRoles(),
      changeRoles,
      can: (permission) => permissions.has(permission),
      grantedBy: (permission) =>
        (Object.keys(catalogue) as Role[]).filter((role) =>
          (catalogue[role] ?? []).includes(permission),
        ),
      refresh,
      ready,
      error,
    };
  }, [mode, estates, estateId, selectEstate, session, changeRoles, refresh, ready, error, roles]);

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}

export function useApp(): AppValue {
  const value = useContext(AppContext);
  if (!value) throw new Error("useApp must be used inside AppProvider");
  return value;
}

/** Permission-gated action metadata, so buttons can explain themselves. */
export function useGate(permission: string): { allowed: boolean; reason: string } {
  const { can, grantedBy } = useApp();
  const allowed = can(permission);
  const roles = grantedBy(permission);
  return {
    allowed,
    reason: allowed
      ? ""
      : `Needs ${permission}. Held by: ${roles.join(", ") || "no role"}. Switch role in the header.`,
  };
}
