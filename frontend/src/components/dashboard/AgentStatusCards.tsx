import {
  ShieldCheck,
  CalendarDays,
  FileText,
  ClipboardCheck,
  Award,
  BarChart3,
} from "lucide-react";
import type { AgentStatsResponse, AgentType } from "../../types";
import { AGENT_LABELS } from "../../types";

interface AgentStatusCardsProps {
  agents: AgentStatsResponse[];
}

const AGENT_ICONS: Record<AgentType, React.ReactNode> = {
  eligibility: <ShieldCheck size={20} className="text-blue-400" />,
  scheduling: <CalendarDays size={20} className="text-purple-400" />,
  claims: <FileText size={20} className="text-amber-400" />,
  prior_auth: <ClipboardCheck size={20} className="text-accent-600" />,
  credentialing: <Award size={20} className="text-indigo-400" />,
  compliance: <BarChart3 size={20} className="text-mint-500" />,
};

const AGENT_BG: Record<AgentType, string> = {
  eligibility: "bg-blue-500/10",
  scheduling: "bg-purple-500/10",
  claims: "bg-amber-500/10",
  prior_auth: "bg-accent-700/10",
  credentialing: "bg-indigo-500/10",
  compliance: "bg-mint-600/10",
};

function StatusBadge({ label, value, color }: { label: string; value: number; color: string }) {
  return (
    <span className={`inline-flex items-center gap-1 text-xs ${value === 0 ? "text-slate-500" : color}`}>
      <span className="font-semibold">{value}</span> {label}
    </span>
  );
}

export default function AgentStatusCards({ agents }: AgentStatusCardsProps) {
  return (
    <div
      className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3"
      data-testid="agent-status-cards"
    >
      {agents.map((agent) => (
        <div
          key={agent.agent_type}
          className="glass-card glass-card-hover rounded-lg p-4"
          data-testid={`agent-card-${agent.agent_type}`}
        >
          <div className="mb-3 flex items-center gap-2">
            <div
              className={`flex h-8 w-8 items-center justify-center rounded-md ${AGENT_BG[agent.agent_type] ?? "bg-slate_d-700"}`}
            >
              {AGENT_ICONS[agent.agent_type] ?? <FileText size={20} />}
            </div>
            <div>
              <h3 className="text-sm font-semibold text-slate-100">
                {AGENT_LABELS[agent.agent_type] ?? agent.agent_type}
              </h3>
              <p className="text-xs text-slate-400">
                {agent.total_tasks} total tasks
              </p>
            </div>
          </div>

          <div className="flex flex-wrap gap-x-3 gap-y-1">
            <StatusBadge label="active" value={agent.running} color="text-blue-400" />
            <StatusBadge label="completed" value={agent.completed} color="text-mint-500" />
            <StatusBadge label="failed" value={agent.failed} color="text-coral-500" />
            <StatusBadge label="pending" value={agent.pending} color="text-yellow-400" />
            <StatusBadge label="review" value={agent.in_review} color="text-orange-400" />
          </div>

          {agent.avg_confidence != null && (
            <div className="mt-2 text-xs text-slate-400">
              Avg confidence:{" "}
              <span className="font-medium text-slate-200">
                {(agent.avg_confidence * 100).toFixed(0)}%
              </span>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
