import { ErrorBoundary } from "./components/ErrorBoundary";
import { Layout } from "./components/Layout";
import { AppProvider } from "./state";
import { Redirect, Router, useRouter } from "./router";
import { EstateScreen } from "./screens/Estate";
import { AssessmentScreen } from "./screens/Assessment";
import { MappingScreen } from "./screens/Mapping";
import { WavesScreen } from "./screens/Waves";
import { PlanScreen } from "./screens/Plan";
import { RunsScreen } from "./screens/Runs";
import { ValidationScreen } from "./screens/Validation";
import { AuditScreen } from "./screens/Audit";
import { ConnectorsScreen } from "./screens/Connectors";

/** The nine screens, keyed by path. Order here is the order of the work. */
const SCREEN_COMPONENTS: Record<string, () => React.ReactElement> = {
  "/estate": EstateScreen,
  "/assessment": AssessmentScreen,
  "/mapping": MappingScreen,
  "/waves": WavesScreen,
  "/plan": PlanScreen,
  "/runs": RunsScreen,
  "/validation": ValidationScreen,
  "/audit": AuditScreen,
  "/connectors": ConnectorsScreen,
};

function Screen() {
  const { path } = useRouter();
  const Component = SCREEN_COMPONENTS[path];
  if (!Component) return <Redirect to="/estate" />;
  // Keyed on the path so navigating away from a broken screen clears the error.
  return (
    <ErrorBoundary key={path}>
      <Component />
    </ErrorBoundary>
  );
}

export function App() {
  return (
    <Router>
      <AppProvider>
        <Layout>
          <Screen />
        </Layout>
      </AppProvider>
    </Router>
  );
}
