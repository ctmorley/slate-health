import { LogOut, User, Wifi, WifiOff } from "lucide-react";
import { useAuthContext } from "../../contexts/AuthContext";

interface HeaderProps {
  isWsConnected: boolean;
}

export default function Header({ isWsConnected }: HeaderProps) {
  const { user, logout } = useAuthContext();

  return (
    <header className="flex h-14 items-center justify-between border-b border-gray-200 bg-white px-6">
      <div className="flex items-center gap-2 text-sm text-gray-500">
        {isWsConnected ? (
          <Wifi size={14} className="text-green-500" />
        ) : (
          <WifiOff size={14} className="text-red-400" />
        )}
        <span>{isWsConnected ? "Connected" : "Disconnected"}</span>
      </div>

      <div className="flex items-center gap-4">
        {user && (
          <div className="flex items-center gap-2 text-sm text-gray-700">
            <User size={16} />
            <span>{user.full_name}</span>
            <span className="rounded bg-gray-100 px-1.5 py-0.5 text-xs text-gray-500">
              {user.roles[0] ?? "user"}
            </span>
          </div>
        )}
        <button
          onClick={logout}
          className="flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm text-gray-600 transition-colors hover:bg-gray-100 hover:text-gray-900"
          title="Logout"
        >
          <LogOut size={16} />
          Logout
        </button>
      </div>
    </header>
  );
}
