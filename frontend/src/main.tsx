import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import "@mantine/core/styles.css";
import "@vscode/codicons/dist/codicon.css";

import { App } from "./App";
import "./styles/cockpit.css";
import "./theme/theme.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
