import { NavLink } from "react-router-dom";
import {
  LayoutDashboard,
  ShieldCheck,
  CalendarDays,
  FileText,
  ClipboardCheck,
  Award,
  BarChart3,
  ListChecks,
  GitBranch,
  BookOpen,
  ScrollText,
} from "lucide-react";
import type { AgentType } from "../../types";
import { AGENT_LABELS } from "../../types";

interface NavItem {
  to: string;
  label: string;
  icon: React.ReactNode;
}

const AGENT_ICONS: Record<AgentType, React.ReactNode> = {
  eligibility: <ShieldCheck size={18} />,
  scheduling: <CalendarDays size={18} />,
  claims: <FileText size={18} />,
  prior_auth: <ClipboardCheck size={18} />,
  credentialing: <Award size={18} />,
  compliance: <BarChart3 size={18} />,
};

const navItems: NavItem[] = [
  { to: "/", label: "Dashboard", icon: <LayoutDashboard size={18} /> },
  ...Object.entries(AGENT_LABELS).map(([type, label]) => ({
    to: `/agents/${type}`,
    label,
    icon: AGENT_ICONS[type as AgentType],
  })),
  { to: "/reviews", label: "Reviews", icon: <ListChecks size={18} /> },
  { to: "/workflows", label: "Workflows", icon: <GitBranch size={18} /> },
  { to: "/payer-rules", label: "Payer Rules", icon: <BookOpen size={18} /> },
  { to: "/audit", label: "Audit Log", icon: <ScrollText size={18} /> },
];

export default function Sidebar() {
  return (
    <aside
      className="flex h-screen w-60 flex-col border-r border-glass bg-slate_d-800"
      data-testid="sidebar"
    >
      <div className="flex h-14 items-center gap-2 border-b border-glass px-4">
        <div className="flex h-8 w-8 items-center justify-center rounded-md bg-indigo-600 text-sm font-bold text-white">
          S
        </div>
        <span className="font-display text-lg font-semibold text-slate-100">
          Slate Health
        </span>
      </div>

      <nav className="flex-1 overflow-y-auto p-2" data-testid="sidebar-nav">
        {navItems.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.to === "/"}
            className={({ isActive }) =>
              `flex items-center gap-2.5 rounded-md px-3 py-2 text-sm font-medium transition-colors ${
                isActive
                  ? "bg-[rgba(99,102,241,0.1)] text-accent-600"
                  : "text-slate-400 hover:bg-slate_d-700 hover:text-slate-200"
              }`
            }
          >
            {item.icon}
            {item.label}
          </NavLink>
        ))}
      </nav>
    </aside>
  );
}
