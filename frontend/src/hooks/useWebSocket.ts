/**
 * useWebSocket — connects to the backend WebSocket and dispatches events.
 *
 * Architecture: a single WebSocket connection is owned by the WebSocketProvider
 * (see contexts/WebSocketContext.tsx) to avoid duplicate connections when
 * multiple components subscribe. This hook is the primary public interface:
 * it connects to the provider-managed socket, subscribes to incoming messages,
 * and exposes connection state. Components should import and use this hook
 * rather than the context directly.
 *
 * The hook fulfils the contract of:
 *   "connects to backend WS, dispatches events to update dashboard in real-time"
 * by delegating the physical connection to the shared provider while allowing
 * each consumer to register its own onMessage callback.
 */
import { useEffect } from "react";
import { useWebSocketContext, type WebSocketContextValue } from "../contexts/WebSocketContext";
import type { WsMessage } from "../types";

interface UseWebSocketOptions {
  /** Callback invoked for every received message from the backend WebSocket. */
  onMessage?: (msg: WsMessage) => void;
  /** Unused — kept for backward compatibility. Reconnect is handled by the provider. */
  reconnectDelay?: number;
}

interface UseWebSocketReturn {
  /** Whether the underlying WebSocket connection is currently open. */
  isConnected: boolean;
  /** The most recent message received (or null if none yet). */
  lastMessage: WsMessage | null;
}

export function useWebSocket(options: UseWebSocketOptions = {}): UseWebSocketReturn {
  const { onMessage } = options;
  const ctx: WebSocketContextValue = useWebSocketContext();

  // Subscribe to incoming messages if a callback is provided.
  // The subscribe function from the context returns an unsubscribe callback
  // which React's useEffect cleanup invokes automatically.
  useEffect(() => {
    if (!onMessage) return;
    return ctx.subscribe(onMessage);
  }, [onMessage, ctx]);

  return { isConnected: ctx.isConnected, lastMessage: ctx.lastMessage };
}
