import { LogOut, User, Wifi, WifiOff } from "lucide-react";
import { useAuthContext } from "../../contexts/AuthContext";

interface HeaderProps {
  isWsConnected: boolean;
}

export default function Header({ isWsConnected }: HeaderProps) {
  const { user, logout } = useAuthContext();

  return (
    <header className="flex h-14 items-center justify-between border-b border-glass bg-slate_d-800 px-6">
      <div className="flex items-center gap-2 text-sm text-slate-400">
        {isWsConnected ? (
          <Wifi size={14} className="text-mint-500" />
        ) : (
          <WifiOff size={14} className="text-coral-500" />
        )}
        <span>{isWsConnected ? "Connected" : "Disconnected"}</span>
      </div>

      <div className="flex items-center gap-4">
        {user && (
          <div className="flex items-center gap-2 text-sm text-slate-300">
            <User size={16} />
            <span>{user.full_name}</span>
            <span className="rounded bg-slate_d-600 px-1.5 py-0.5 text-xs text-slate-400">
              {user.roles[0] ?? "user"}
            </span>
          </div>
        )}
        <button
          onClick={logout}
          className="flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm text-slate-400 transition-colors hover:bg-slate_d-700 hover:text-slate-200"
          title="Logout"
        >
          <LogOut size={16} />
          Logout
        </button>
      </div>
    </header>
  );
}
