import { useCallback, useMemo, useRef, useState } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { AppContext } from "./app-context";
import { Layout } from "./components/Layout";
import { AdminPage } from "./pages/AdminPage";
import { LibraryPage } from "./pages/LibraryPage";
import { ResearchPage } from "./pages/ResearchPage";

interface ToastState {
  message: string;
  tone: "success" | "error" | "info";
  visible: boolean;
}

export default function App(): React.JSX.Element {
  const [toast, setToast] = useState<ToastState>({ message: "", tone: "info", visible: false });
  const timer = useRef<number | null>(null);

  const notify = useCallback((message: string, tone: ToastState["tone"] = "info") => {
    if (timer.current) window.clearTimeout(timer.current);
    setToast({ message, tone, visible: true });
    timer.current = window.setTimeout(() => {
      setToast((current) => ({ ...current, visible: false }));
    }, 3400);
  }, []);

  const context = useMemo(
    () => ({ notify, openAccessKey: () => window.dispatchEvent(new Event("atlas:open-key")) }),
    [notify],
  );

  return (
    <AppContext.Provider value={context}>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<Navigate to="/workbench" replace />} />
          <Route path="/workbench" element={<ResearchPage />} />
          <Route path="/library" element={<LibraryPage />} />
          <Route path="/admin" element={<AdminPage />} />
          <Route path="*" element={<Navigate to="/workbench" replace />} />
        </Route>
      </Routes>
      <div className={`toast toast-${toast.tone} ${toast.visible ? "toast-visible" : ""}`} role="status" aria-live="polite">
        <i /> {toast.message}
      </div>
    </AppContext.Provider>
  );
}
