import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { startTranslation } from "./i18n";
import "./tokens.css";
import "./style.css";
import "./workbench.css";
startTranslation();
createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
