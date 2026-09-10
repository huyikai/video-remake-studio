import { useEffect } from "react";
import { useUi, type DialogKind } from "../ui";

export default function OpenDialog({ kind }: { kind: Exclude<DialogKind, null> }) {
  const { setOpen } = useUi();
  useEffect(() => {
    setOpen(kind);
  }, [kind, setOpen]);
  return null;
}
