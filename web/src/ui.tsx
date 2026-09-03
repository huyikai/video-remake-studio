import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";
import NewJob from "./pages/NewJob";
import SettingsPage from "./pages/Settings";

export type DialogKind = null | "new" | "settings";

type UiValue = {
  open: DialogKind;
  setOpen: (kind: DialogKind) => void;
};

const UiContext = createContext<UiValue>({ open: null, setOpen: () => undefined });

export function useUi() {
  return useContext(UiContext);
}

export function UiProvider({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState<DialogKind>(null);
  const close = useCallback(() => setOpen(null), []);
  const value = useMemo(() => ({ open, setOpen }), [open]);
  return (
    <UiContext.Provider value={value}>
      {children}
      {open === "new" ? <NewJob onClose={close} /> : null}
      {open === "settings" ? <SettingsPage onClose={close} /> : null}
    </UiContext.Provider>
  );
}
