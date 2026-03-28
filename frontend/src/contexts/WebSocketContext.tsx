import {
  createContext,
  useContext,
  useEffect,
  useRef,
  useState,
  useCallback,
  type ReactNode,
} from "react";
import { useAuthContext } from "./AuthContext";
import type { WsMessage } from "../types";

type WsListener = (msg: WsMessage) => void;

export interface WebSocketContextValue {
  isConnected: boolean;
  lastMessage: WsMessage | null;
  /** Subscribe to incoming messages. Returns an unsubscribe function. */
  subscribe: (listener: WsListener) => () => void;
}

const WebSocketContext = createContext<WebSocketContextValue | null>(null);

/** Maximum number of consecutive reconnect attempts before entering cooldown. */
const MAX_RECONNECT_ATTEMPTS = 10;
/** Base delay in ms for exponential backoff (doubles each attempt, capped at 30s). */
const BASE_RECONNECT_DELAY_MS = 1000;
const MAX_RECONNECT_DELAY_MS = 30_000;
/** After exhausting MAX_RECONNECT_ATTEMPTS, wait this long then retry a fresh cycle. */
const RECOVERY_COOLDOWN_MS = 60_000;

export function WebSocketProvider({ children }: { children: ReactNode }) {
  const [isConnected, setIsConnected] = useState(false);
  const [lastMessage, setLastMessage] = useState<WsMessage | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout>>();
  const listenersRef = useRef<Set<WsListener>>(new Set());
  const unmountedRef = useRef(false);
  const reconnectAttemptRef = useRef(0);

  // React to auth state changes so the WebSocket connects/disconnects
  // when the user logs in or out.
  const { isAuthenticated } = useAuthContext();

  const subscribe = useCallback((listener: WsListener) => {
    listenersRef.current.add(listener);
    return () => {
      listenersRef.current.delete(listener);
    };
  }, []);

  useEffect(() => {
    unmountedRef.current = false;
    reconnectAttemptRef.current = 0;

    // If the user is not authenticated, don't attempt to connect.
    // When isAuthenticated changes to true (post-login), this effect
    // will re-run and establish the connection.
    if (!isAuthenticated) {
      // Clean up any existing connection from a previous session
      clearTimeout(reconnectTimer.current);
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
      setIsConnected(false);
      return;
    }

    function connect() {
      const token = localStorage.getItem("access_token");
      if (!token || unmountedRef.current) return;

      // All immediate retries exhausted — enter recovery cooldown and
      // then start a fresh reconnect cycle. This ensures we never silently
      // stop reconnecting; after a cooldown we try again from scratch.
      // Clear any previously scheduled timer to avoid double-scheduling
      // (e.g. if onclose also schedules a cooldown for the same cycle).
      if (reconnectAttemptRef.current >= MAX_RECONNECT_ATTEMPTS) {
        clearTimeout(reconnectTimer.current);
        reconnectTimer.current = setTimeout(() => {
          if (!unmountedRef.current) {
            reconnectAttemptRef.current = 0;
            connect();
          }
        }, RECOVERY_COOLDOWN_MS);
        return;
      }

      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const host = import.meta.env.VITE_WS_HOST ?? window.location.host;
      const url = `${protocol}//${host}/api/v1/ws/events?token=${encodeURIComponent(token)}`;

      const ws = new WebSocket(url);
      wsRef.current = ws;

      ws.onopen = () => {
        if (!unmountedRef.current) {
          setIsConnected(true);
          // Reset reconnect counter on successful connection
          reconnectAttemptRef.current = 0;
        }
      };

      ws.onmessage = (event) => {
        if (unmountedRef.current) return;
        try {
          const msg: WsMessage = JSON.parse(event.data);
          setLastMessage(msg);
          listenersRef.current.forEach((fn) => fn(msg));
        } catch {
          // ignore non-JSON messages
        }
      };

      ws.onclose = (event) => {
        if (unmountedRef.current) return;
        setIsConnected(false);

        // 4001/4003/1008 = auth failure codes — token is invalid.
        // Clear stored credentials and force redirect to login so the UI
        // doesn't appear logged-in while permanently disconnected.
        if (event.code === 4001 || event.code === 4003 || event.code === 1008) {
          localStorage.removeItem("access_token");
          localStorage.removeItem("refresh_token");
          window.location.href = "/login";
          return;
        }

        // Exponential backoff with cap
        if (reconnectAttemptRef.current < MAX_RECONNECT_ATTEMPTS) {
          const delay = Math.min(
            BASE_RECONNECT_DELAY_MS * Math.pow(2, reconnectAttemptRef.current),
            MAX_RECONNECT_DELAY_MS,
          );
          reconnectAttemptRef.current += 1;
          reconnectTimer.current = setTimeout(() => {
            if (!unmountedRef.current) connect();
          }, delay);
        } else {
          // All immediate retries exhausted — enter recovery cooldown and
          // then start a fresh reconnect cycle. This prevents giving up
          // permanently after transient long outages. Clear any existing
          // cooldown timer (e.g. one scheduled by connect()) to avoid
          // double-firing.
          clearTimeout(reconnectTimer.current);
          reconnectTimer.current = setTimeout(() => {
            if (!unmountedRef.current) {
              reconnectAttemptRef.current = 0;
              connect();
            }
          }, RECOVERY_COOLDOWN_MS);
        }
      };

      ws.onerror = () => {
        ws.close();
      };
    }

    connect();

    // Ping keep-alive every 30s
    const pingInterval = setInterval(() => {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send("ping");
      }
    }, 30_000);

    return () => {
      unmountedRef.current = true;
      clearInterval(pingInterval);
      clearTimeout(reconnectTimer.current);
      wsRef.current?.close();
    };
  }, [isAuthenticated]);

  return (
    <WebSocketContext.Provider value={{ isConnected, lastMessage, subscribe }}>
      {children}
    </WebSocketContext.Provider>
  );
}

export function useWebSocketContext(): WebSocketContextValue {
  const ctx = useContext(WebSocketContext);
  if (!ctx) {
    throw new Error("useWebSocketContext must be used within a WebSocketProvider");
  }
  return ctx;
}
