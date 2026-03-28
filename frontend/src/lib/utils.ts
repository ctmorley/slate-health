import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

/**
 * Merge Tailwind CSS classes with proper conflict resolution.
 * This is the standard `cn` utility used by shadcn/ui components.
 */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
