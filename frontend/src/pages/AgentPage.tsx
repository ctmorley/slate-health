import { useState, useCallback } from "react";
import { useParams } from "react-router-dom";
import { Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import AgentTaskList from "@/components/agents/AgentTaskList";
import AgentTaskDetail from "@/components/agents/AgentTaskDetail";
import NewTaskForm from "@/components/agents/NewTaskForm";
import type { AgentType, AgentTaskResponse } from "@/types";
import { AGENT_LABELS, AGENT_TYPES } from "@/types";

type View = "list" | "detail" | "create";

export default function AgentPage() {
  const { agentType } = useParams<{ agentType: string }>();
  const [view, setView] = useState<View>("list");
  const [selectedTask, setSelectedTask] = useState<AgentTaskResponse | null>(
    null,
  );
  const [listKey, setListKey] = useState(0);

  // Validate agent type
  const validAgentType = AGENT_TYPES.includes(agentType as AgentType)
    ? (agentType as AgentType)
    : null;

  const label = validAgentType
    ? AGENT_LABELS[validAgentType]
    : agentType ?? "Unknown";

  const handleSelectTask = useCallback((task: AgentTaskResponse) => {
    setSelectedTask(task);
    setView("detail");
  }, []);

  const handleBackToList = useCallback(() => {
    setSelectedTask(null);
    setView("list");
  }, []);

  const handleTaskCreated = useCallback(() => {
    setView("list");
    // Force re-mount of task list to refresh data
    setListKey((k) => k + 1);
  }, []);

  if (!validAgentType) {
    return (
      <div>
        <h1 className="text-xl font-semibold text-gray-900">
          Unknown Agent Type
        </h1>
        <p className="mt-1 text-sm text-gray-500">
          The agent type &quot;{agentType}&quot; is not recognized.
        </p>
      </div>
    );
  }

  return (
    <div>
      {/* Page header — only shown on list view */}
      {view === "list" && (
        <div className="mb-6 flex items-center justify-between">
          <div>
            <h1 className="text-xl font-semibold text-gray-900">
              {label} Agent
            </h1>
            <p className="mt-1 text-sm text-gray-500">
              Task list and management for the {label} agent.
            </p>
          </div>
          <Button onClick={() => setView("create")} data-testid="new-task-button">
            <Plus size={16} />
            New Task
          </Button>
        </div>
      )}

      {view === "list" && (
        <AgentTaskList
          key={listKey}
          agentType={validAgentType}
          onSelectTask={handleSelectTask}
        />
      )}

      {view === "detail" && selectedTask && (
        <AgentTaskDetail
          agentType={validAgentType}
          taskId={selectedTask.task_id}
          onBack={handleBackToList}
        />
      )}

      {view === "create" && (
        <NewTaskForm
          agentType={validAgentType}
          onCreated={handleTaskCreated}
          onCancel={handleBackToList}
        />
      )}
    </div>
  );
}
