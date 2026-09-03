import { useEffect } from "react";
import { Navigate } from "react-router-dom";
import { useUi, type DialogKind } from "../ui";

export default function OpenDialog({ kind }: { kind: Exclude<DialogKind, null> }) {
  const { setOpen } = useUi();
  useEffect(() => {
    setOpen(kind);
  }, [kind, setOpen]);
  return <Navigate to="/" replace />;
}
