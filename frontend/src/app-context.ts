import { createContext, useContext } from "react";

export interface AppContextValue {
  notify: (message: string, tone?: "success" | "error" | "info") => void;
  openAccessKey: () => void;
}

export const AppContext = createContext<AppContextValue | null>(null);

export function useApp(): AppContextValue {
  const value = useContext(AppContext);
  if (!value) throw new Error("AppContext is unavailable");
  return value;
}
