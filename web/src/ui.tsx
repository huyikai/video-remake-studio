import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";
import { useLocation, useNavigate } from "react-router-dom";
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
  const nav = useNavigate();
  const location = useLocation();
  const close = useCallback(() => {
    setOpen(null);
    const onDialogRoute = location.pathname === "/new" || location.pathname === "/settings";
    if (onDialogRoute) nav(-1);
  }, [nav, location.pathname]);
  const value = useMemo(() => ({ open, setOpen }), [open]);
  return (
    <UiContext.Provider value={value}>
      {children}
      {open === "new" ? <NewJob onClose={close} /> : null}
      {open === "settings" ? <SettingsPage onClose={close} /> : null}
    </UiContext.Provider>
  );
}
