import axios, { AxiosError, InternalAxiosRequestConfig } from "axios";

const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "";

const client = axios.create({
  baseURL: BASE_URL,
  headers: { "Content-Type": "application/json" },
});

// ─── Request interceptor: attach JWT ─────────────────────────────────────────
client.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const token = localStorage.getItem("access_token");
  if (token && config.headers) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// ─── Response interceptor: handle 401 with token refresh ─────────────────────
let isRefreshing = false;
let failedQueue: Array<{
  resolve: (token: string) => void;
  reject: (err: unknown) => void;
}> = [];

function processQueue(error: unknown, token: string | null) {
  failedQueue.forEach((p) => {
    if (error) {
      p.reject(error);
    } else {
      p.resolve(token!);
    }
  });
  failedQueue = [];
}

client.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => {
    const originalRequest = error.config as InternalAxiosRequestConfig & {
      _retry?: boolean;
    };

    if (error.response?.status !== 401 || originalRequest._retry) {
      return Promise.reject(error);
    }

    const refreshToken = localStorage.getItem("refresh_token");
    if (!refreshToken) {
      // No refresh token — force logout
      localStorage.removeItem("access_token");
      localStorage.removeItem("refresh_token");
      window.location.href = "/login";
      return Promise.reject(error);
    }

    if (isRefreshing) {
      // Another refresh is already in-flight; queue this request
      return new Promise<string>((resolve, reject) => {
        failedQueue.push({ resolve, reject });
      }).then((token) => {
        if (originalRequest.headers) {
          originalRequest.headers.Authorization = `Bearer ${token}`;
        }
        return client(originalRequest);
      });
    }

    originalRequest._retry = true;
    isRefreshing = true;

    try {
      const { data } = await axios.post(`${BASE_URL}/api/v1/auth/refresh`, {
        refresh_token: refreshToken,
      });
      const newAccessToken: string = data.access_token;
      localStorage.setItem("access_token", newAccessToken);
      if (data.refresh_token) {
        localStorage.setItem("refresh_token", data.refresh_token);
      }
      processQueue(null, newAccessToken);
      if (originalRequest.headers) {
        originalRequest.headers.Authorization = `Bearer ${newAccessToken}`;
      }
      return client(originalRequest);
    } catch (refreshError) {
      processQueue(refreshError, null);
      localStorage.removeItem("access_token");
      localStorage.removeItem("refresh_token");
      window.location.href = "/login";
      return Promise.reject(refreshError);
    } finally {
      isRefreshing = false;
    }
  },
);

export default client;
