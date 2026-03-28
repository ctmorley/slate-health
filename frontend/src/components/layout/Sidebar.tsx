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
      className="flex h-screen w-60 flex-col border-r border-gray-200 bg-white"
      data-testid="sidebar"
    >
      <div className="flex h-14 items-center gap-2 border-b border-gray-200 px-4">
        <div className="flex h-8 w-8 items-center justify-center rounded-md bg-teal-700 text-sm font-bold text-white">
          S
        </div>
        <span className="text-lg font-semibold text-gray-900">
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
                  ? "bg-teal-50 text-teal-700"
                  : "text-gray-600 hover:bg-gray-50 hover:text-gray-900"
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
