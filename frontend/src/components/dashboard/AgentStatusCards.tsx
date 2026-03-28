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
  eligibility: <ShieldCheck size={20} className="text-blue-600" />,
  scheduling: <CalendarDays size={20} className="text-purple-600" />,
  claims: <FileText size={20} className="text-amber-600" />,
  prior_auth: <ClipboardCheck size={20} className="text-teal-600" />,
  credentialing: <Award size={20} className="text-indigo-600" />,
  compliance: <BarChart3 size={20} className="text-emerald-600" />,
};

const AGENT_BG: Record<AgentType, string> = {
  eligibility: "bg-blue-50",
  scheduling: "bg-purple-50",
  claims: "bg-amber-50",
  prior_auth: "bg-teal-50",
  credentialing: "bg-indigo-50",
  compliance: "bg-emerald-50",
};

function StatusBadge({ label, value, color }: { label: string; value: number; color: string }) {
  return (
    <span className={`inline-flex items-center gap-1 text-xs ${value === 0 ? "text-gray-400" : color}`}>
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
          className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm"
          data-testid={`agent-card-${agent.agent_type}`}
        >
          <div className="mb-3 flex items-center gap-2">
            <div
              className={`flex h-8 w-8 items-center justify-center rounded-md ${AGENT_BG[agent.agent_type] ?? "bg-gray-50"}`}
            >
              {AGENT_ICONS[agent.agent_type] ?? <FileText size={20} />}
            </div>
            <div>
              <h3 className="text-sm font-semibold text-gray-900">
                {AGENT_LABELS[agent.agent_type] ?? agent.agent_type}
              </h3>
              <p className="text-xs text-gray-500">
                {agent.total_tasks} total tasks
              </p>
            </div>
          </div>

          <div className="flex flex-wrap gap-x-3 gap-y-1">
            <StatusBadge label="active" value={agent.running} color="text-blue-600" />
            <StatusBadge label="completed" value={agent.completed} color="text-green-600" />
            <StatusBadge label="failed" value={agent.failed} color="text-red-600" />
            <StatusBadge label="pending" value={agent.pending} color="text-yellow-600" />
            <StatusBadge label="review" value={agent.in_review} color="text-orange-600" />
          </div>

          {agent.avg_confidence != null && (
            <div className="mt-2 text-xs text-gray-500">
              Avg confidence:{" "}
              <span className="font-medium text-gray-700">
                {(agent.avg_confidence * 100).toFixed(0)}%
              </span>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
