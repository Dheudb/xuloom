import { createContext } from "react";
import type { FileReference } from "./references";
export const ReferenceNavigation = createContext<((target: FileReference, remoteUrl?: string) => Promise<void>) | undefined>(undefined);
