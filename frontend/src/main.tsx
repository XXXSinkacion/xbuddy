import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./styles.css";
import { applyTheme, readStoredTheme } from "./theme";

applyTheme(readStoredTheme()); // 渲染前设置主题，避免闪烁

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
