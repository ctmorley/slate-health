import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useAuth } from "../hooks/useAuth";
import { fetchLoginProviders } from "../api/auth";

type SsoProvider = "saml" | "oidc";

const PROVIDER_CONFIG: Record<SsoProvider, { label: string; color: string; letter: string }> = {
  saml: { label: "Sign in with SAML SSO", color: "#4F46E5", letter: "S" },
  oidc: { label: "Sign in with OpenID Connect", color: "#059669", letter: "O" },
};

export default function LoginPage() {
  const { login, isAuthenticated, handleAuthCallback, error } = useAuth();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();

  const [providers, setProviders] = useState<SsoProvider[]>([]);
  const [providersLoading, setProvidersLoading] = useState(true);
  const [providersError, setProvidersError] = useState<string | null>(null);

  // Fetch available providers from backend on mount
  useEffect(() => {
    let cancelled = false;
    setProvidersLoading(true);
    setProvidersError(null);

    fetchLoginProviders()
      .then((info) => {
        if (!cancelled) {
          setProviders(info.providers);
        }
      })
      .catch(() => {
        if (!cancelled) {
          // Graceful fallback: if backend is unreachable, show both providers
          // so the user can still attempt login. The login POST will fail with
          // a clearer error message if the provider isn't actually configured.
          setProviders(["saml", "oidc"]);
          setProvidersError("Could not reach server to check available providers");
        }
      })
      .finally(() => {
        if (!cancelled) setProvidersLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, []);

  // Handle SSO callback with token in query params.
  // Immediately scrub tokens from the URL to prevent them leaking via
  // browser history or Referer headers.
  useEffect(() => {
    const accessToken = searchParams.get("access_token");
    const refreshToken = searchParams.get("refresh_token");
    if (accessToken && refreshToken) {
      // Remove tokens from the visible URL before processing
      const cleanUrl = `${window.location.pathname}`;
      window.history.replaceState({}, "", cleanUrl);
      handleAuthCallback(accessToken, refreshToken);
    }
  }, [searchParams, handleAuthCallback]);

  // Redirect if already authenticated
  useEffect(() => {
    if (isAuthenticated) {
      navigate("/", { replace: true });
    }
  }, [isAuthenticated, navigate]);

  return (
    <div className="flex min-h-screen items-center justify-center bg-gray-50">
      <div className="w-full max-w-sm rounded-xl border border-gray-200 bg-white p-8 shadow-sm">
        <div className="mb-6 text-center">
          <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-lg bg-teal-700 text-xl font-bold text-white">
            S
          </div>
          <h1 className="text-xl font-semibold text-gray-900">Slate Health</h1>
          <p className="mt-1 text-sm text-gray-500">
            Sign in to your account
          </p>
        </div>

        {error && (
          <div
            className="mb-4 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700"
            role="alert"
            data-testid="login-error"
          >
            {error}
          </div>
        )}

        {providersError && !error && (
          <div
            className="mb-4 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-700"
            role="status"
            data-testid="providers-warning"
          >
            {providersError}
          </div>
        )}

        <div className="space-y-3" data-testid="sso-provider-list">
          {providersLoading ? (
            <div className="flex justify-center py-4" data-testid="providers-loading">
              <div className="h-6 w-6 animate-spin rounded-full border-2 border-teal-600 border-t-transparent" />
            </div>
          ) : (
            providers.map((provider) => {
              const cfg = PROVIDER_CONFIG[provider];
              return (
                <button
                  key={provider}
                  onClick={() => login(provider)}
                  className="flex w-full items-center justify-center gap-2 rounded-lg border border-gray-300 bg-white px-4 py-2.5 text-sm font-medium text-gray-700 transition-colors hover:bg-gray-50"
                  data-testid={`sso-${provider}-button`}
                >
                  <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
                    <rect width="18" height="18" rx="3" fill={cfg.color} />
                    <text
                      x="9"
                      y="13"
                      textAnchor="middle"
                      fill="white"
                      fontSize="10"
                      fontWeight="bold"
                    >
                      {cfg.letter}
                    </text>
                  </svg>
                  {cfg.label}
                </button>
              );
            })
          )}
        </div>

        <p className="mt-6 text-center text-xs text-gray-400">
          HIPAA-compliant healthcare AI platform
        </p>
      </div>
    </div>
  );
}
