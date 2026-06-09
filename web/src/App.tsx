import { Suspense } from "react";
import { useRoutes } from "react-router-dom";

import { routes } from "./routes";

/** The routed app body. Providers (Query, Router) wrap this in main.tsx. */
export function App(): React.JSX.Element {
  const element = useRoutes(routes);
  return <Suspense fallback={null}>{element}</Suspense>;
}
