import Sidebar from "./Sidebar";
import Header from "./Header";
import { useWebSocketContext } from "../../contexts/WebSocketContext";

interface LayoutProps {
  children: React.ReactNode;
}

export default function Layout({ children }: LayoutProps) {
  const { isConnected } = useWebSocketContext();

  return (
    <div className="flex h-screen overflow-hidden">
      <Sidebar />
      <div className="flex flex-1 flex-col overflow-hidden">
        <Header isWsConnected={isConnected} />
        <main className="flex-1 overflow-y-auto bg-gray-50 p-6">
          {children}
        </main>
      </div>
    </div>
  );
}
