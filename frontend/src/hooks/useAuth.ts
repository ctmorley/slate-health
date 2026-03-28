/**
 * Public auth hook — the single entry-point for components that need
 * authentication state and actions.
 *
 * Wraps the internal AuthContext and re-exports individual values so that
 * consumers can destructure only what they need.  This hook IS the public
 * auth API surface; AuthContext/AuthProvider are implementation details.
 */
import { useAuthContext } from "../contexts/AuthContext";
import type { UserProfile } from "../types";

export interface UseAuthReturn {
  /** The currently logged-in user, or null if unauthenticated. */
  user: UserProfile | null;
  /** Whether a valid user session is active. */
  isAuthenticated: boolean;
  /** True while the initial token check / user fetch is in progress. */
  isLoading: boolean;
  /** Last auth-related error message, or null. */
  error: string | null;
  /** Initiate SSO login by redirecting to the given IdP provider. */
  login: (provider: "saml" | "oidc") => void;
  /**
   * Process an SSO callback — stores tokens, fetches user profile,
   * and transitions to the authenticated state.
   */
  handleAuthCallback: (accessToken: string, refreshToken: string) => void;
  /** Clear tokens, reset state, and redirect to the login page. */
  logout: () => void;
}

export function useAuth(): UseAuthReturn {
  const ctx = useAuthContext();
  return {
    user: ctx.user,
    isAuthenticated: ctx.isAuthenticated,
    isLoading: ctx.isLoading,
    error: ctx.error,
    login: ctx.login,
    handleAuthCallback: ctx.handleAuthCallback,
    logout: ctx.logout,
  };
}
