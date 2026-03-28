import { createContext, useContext, useState, useEffect, useCallback, type ReactNode } from "react";
import type { UserProfile } from "../types";
import { initiateLogin, fetchCurrentUser } from "../api/auth";

export interface AuthContextValue {
  user: UserProfile | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  error: string | null;
  login: (provider: "saml" | "oidc") => void;
  handleAuthCallback: (accessToken: string, refreshToken: string) => void;
  logout: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<UserProfile | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const isAuthenticated = user !== null;

  // On mount, check for stored token and fetch user profile
  useEffect(() => {
    const token = localStorage.getItem("access_token");
    if (!token) {
      setIsLoading(false);
      return;
    }

    let cancelled = false;
    fetchCurrentUser()
      .then((profile) => {
        if (!cancelled) setUser(profile);
      })
      .catch(() => {
        localStorage.removeItem("access_token");
        localStorage.removeItem("refresh_token");
        if (!cancelled) setUser(null);
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, []);

  const login = useCallback((provider: "saml" | "oidc") => {
    const callbackUrl = `${window.location.origin}/login`;
    initiateLogin({ provider, redirect_url: callbackUrl })
      .then((response) => {
        window.location.assign(response.redirect_url);
      })
      .catch((err) => {
        setError(
          err?.response?.data?.detail ??
            err?.message ??
            "Failed to initiate SSO login",
        );
      });
  }, []);

  const handleAuthCallback = useCallback(
    (accessToken: string, refreshToken: string) => {
      localStorage.setItem("access_token", accessToken);
      localStorage.setItem("refresh_token", refreshToken);
      setIsLoading(true);
      setError(null);
      fetchCurrentUser()
        .then((profile) => setUser(profile))
        .catch((err) => {
          // Clear tokens AND user so the app doesn't retain invalid credentials
          // or show stale authenticated UI from a previous session.
          localStorage.removeItem("access_token");
          localStorage.removeItem("refresh_token");
          setUser(null);
          setError(err.message ?? "Failed to load user profile");
        })
        .finally(() => setIsLoading(false));
    },
    [],
  );

  const logout = useCallback(() => {
    localStorage.removeItem("access_token");
    localStorage.removeItem("refresh_token");
    setUser(null);
    window.location.href = "/login";
  }, []);

  return (
    <AuthContext.Provider
      value={{ user, isAuthenticated, isLoading, error, login, handleAuthCallback, logout }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuthContext(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error("useAuthContext must be used within an AuthProvider");
  }
  return ctx;
}
