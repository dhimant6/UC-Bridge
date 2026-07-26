/**
 * A minimal History-API router.
 *
 * This console has nine flat routes and needs nothing else — no nested layouts,
 * no loaders, no data APIs. `react-router` was the obvious choice until every
 * published 7.x fell under GHSA-qwww-vcr4-c8h2, with no patched release to
 * upgrade to. Rather than ship a known-vulnerable dependency or pin to an
 * unmaintained version for features we do not use, the forty lines it would have
 * saved live here instead.
 *
 * The server serves `index.html` for unknown paths (see ``SpaFiles``), so a
 * refresh on ``/waves`` lands back here with the path intact.
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { MouseEvent, ReactNode } from "react";

interface RouterValue {
  path: string;
  navigate: (to: string, options?: { replace?: boolean }) => void;
}

const RouterContext = createContext<RouterValue | null>(null);

export function Router({ children }: { children: ReactNode }) {
  const [path, setPath] = useState(() => window.location.pathname);

  useEffect(() => {
    const onPopState = () => setPath(window.location.pathname);
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  const navigate = useCallback((to: string, options?: { replace?: boolean }) => {
    if (to === window.location.pathname) return;
    if (options?.replace) {
      window.history.replaceState(null, "", to);
    } else {
      window.history.pushState(null, "", to);
    }
    setPath(to);
  }, []);

  const value = useMemo(() => ({ path, navigate }), [path, navigate]);
  return <RouterContext.Provider value={value}>{children}</RouterContext.Provider>;
}

export function useRouter(): RouterValue {
  const value = useContext(RouterContext);
  if (!value) throw new Error("useRouter must be used inside Router");
  return value;
}

/** An internal link. Modified clicks fall through to the browser. */
export function Link({
  to,
  className,
  children,
}: {
  to: string;
  className?: string | ((isActive: boolean) => string);
  children: ReactNode;
}) {
  const { path, navigate } = useRouter();
  const isActive = path === to;

  const onClick = (event: MouseEvent<HTMLAnchorElement>) => {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) {
      return;
    }
    event.preventDefault();
    navigate(to);
  };

  return (
    <a
      href={to}
      onClick={onClick}
      className={typeof className === "function" ? className(isActive) : className}
      aria-current={isActive ? "page" : undefined}
    >
      {children}
    </a>
  );
}

/** Redirect on mount. Used for `/` and for anything unrecognised. */
export function Redirect({ to }: { to: string }) {
  const { navigate } = useRouter();
  useEffect(() => {
    navigate(to, { replace: true });
  }, [navigate, to]);
  return null;
}
