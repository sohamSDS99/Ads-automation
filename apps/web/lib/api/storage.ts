/** Volume usage and the retention windows applied to it (PRD §13.4 E, §16). */
import { apiFetch } from "@/lib/api";

export type StorageUsage = {
  objects: number;
  bytes: number;
  capacity_bytes: number | null;
  used_fraction: number | null;
  warn_above: number;
  at_capacity: boolean;
  /** False when the worker could not be reached. Never render a zero for this. */
  reachable: boolean;
  /** Days kept, per prefix. 0 means keep forever. */
  retention: Record<string, number>;
};

export function getStorageUsage(): Promise<StorageUsage> {
  return apiFetch("/storage");
}
