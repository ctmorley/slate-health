import { useState, useEffect, useCallback } from "react";
import { BookOpen, Save, X, ChevronRight, Plus, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  listPayers,
  listPayerRules,
  updatePayerRule,
  createPayerRule,
} from "@/api/payers";
import type {
  PayerResponse,
  PayerRuleResponse,
  PayerRuleCreate,
  AgentType,
} from "@/types";
import { AGENT_LABELS, AGENT_TYPES } from "@/types";

export default function PayerRulesPage() {
  const [payers, setPayers] = useState<PayerResponse[]>([]);
  const [selectedPayer, setSelectedPayer] = useState<PayerResponse | null>(
    null,
  );
  const [rules, setRules] = useState<PayerRuleResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [rulesLoading, setRulesLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Edit state
  const [editingRule, setEditingRule] = useState<PayerRuleResponse | null>(
    null,
  );
  const [editJson, setEditJson] = useState("");
  const [editDescription, setEditDescription] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveSuccess, setSaveSuccess] = useState<string | null>(null);

  // New rule state
  const [showNewRule, setShowNewRule] = useState(false);
  const [newRuleType, setNewRuleType] = useState("");
  const [newRuleAgent, setNewRuleAgent] = useState<AgentType>("eligibility");
  const [newRuleDesc, setNewRuleDesc] = useState("");
  const [newRuleJson, setNewRuleJson] = useState("{}");
  const [newRuleDate, setNewRuleDate] = useState(
    new Date().toISOString().slice(0, 10),
  );

  // Fetch payers
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    listPayers()
      .then((data) => {
        if (!cancelled) setPayers(data);
      })
      .catch((err) => {
        if (!cancelled)
          setError(
            err instanceof Error ? err.message : "Failed to load payers",
          );
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const [rulesError, setRulesError] = useState<string | null>(null);

  // Fetch rules for selected payer
  const fetchRules = useCallback(async (payerId: string) => {
    setRulesLoading(true);
    setRulesError(null);
    try {
      const data = await listPayerRules(payerId);
      setRules(data);
    } catch (err) {
      setRules([]);
      setRulesError(err instanceof Error ? err.message : "Failed to load rules for this payer");
    } finally {
      setRulesLoading(false);
    }
  }, []);

  function handleSelectPayer(payer: PayerResponse) {
    setSelectedPayer(payer);
    setEditingRule(null);
    setShowNewRule(false);
    fetchRules(payer.id);
  }

  function startEdit(rule: PayerRuleResponse) {
    setEditingRule(rule);
    setEditJson(JSON.stringify(rule.conditions, null, 2));
    setEditDescription(rule.description ?? "");
    setSaveError(null);
    setSaveSuccess(null);
    setShowNewRule(false);
  }

  async function handleSave() {
    if (!editingRule || !selectedPayer) return;
    setSaving(true);
    setSaveError(null);
    setSaveSuccess(null);
    try {
      const conditions = JSON.parse(editJson);
      await updatePayerRule(selectedPayer.id, editingRule.id, {
        conditions,
        description: editDescription || null,
      });
      setSaveSuccess("Rule updated successfully");
      setEditingRule(null);
      fetchRules(selectedPayer.id);
    } catch (err) {
      setSaveError(
        err instanceof Error ? err.message : "Failed to save rule",
      );
    } finally {
      setSaving(false);
    }
  }

  async function handleCreateRule() {
    if (!selectedPayer) return;
    setSaving(true);
    setSaveError(null);
    try {
      const conditions = JSON.parse(newRuleJson);
      const rule: PayerRuleCreate = {
        agent_type: newRuleAgent,
        rule_type: newRuleType,
        description: newRuleDesc || null,
        conditions,
        effective_date: newRuleDate,
      };
      await createPayerRule(selectedPayer.id, rule);
      setShowNewRule(false);
      setNewRuleType("");
      setNewRuleDesc("");
      setNewRuleJson("{}");
      fetchRules(selectedPayer.id);
    } catch (err) {
      setSaveError(
        err instanceof Error ? err.message : "Failed to create rule",
      );
    } finally {
      setSaving(false);
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <div className="h-6 w-6 animate-spin rounded-full border-4 border-teal-600 border-t-transparent" />
      </div>
    );
  }

  return (
    <div>
      <div className="mb-6">
        <h1 className="flex items-center gap-2 text-xl font-semibold text-gray-900">
          <BookOpen size={22} />
          Payer Rules
        </h1>
        <p className="mt-1 text-sm text-gray-500">
          Manage payer-specific rules and configurations.
        </p>
      </div>

      {error && (
        <div className="mb-4 rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">
          {error}
        </div>
      )}

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        {/* Payer list */}
        <div className="lg:col-span-1">
          <div className="rounded-lg border border-gray-200 bg-white" data-testid="payer-list">
            <div className="border-b border-gray-200 px-4 py-3">
              <h3 className="text-sm font-semibold text-gray-900">Payers</h3>
            </div>
            {payers.length === 0 ? (
              <p className="p-4 text-center text-sm text-gray-400" data-testid="payer-list-empty">
                No payers configured
              </p>
            ) : (
              <ul className="divide-y divide-gray-100">
                {payers.map((payer) => (
                  <li
                    key={payer.id}
                    onClick={() => handleSelectPayer(payer)}
                    className={`flex cursor-pointer items-center justify-between px-4 py-3 transition-colors hover:bg-gray-50 ${
                      selectedPayer?.id === payer.id ? "bg-teal-50" : ""
                    }`}
                    data-testid={`payer-item-${payer.id}`}
                  >
                    <div>
                      <p className="text-sm font-medium text-gray-900">
                        {payer.name}
                      </p>
                      <p className="text-xs text-gray-500">
                        {payer.payer_id_code}
                      </p>
                    </div>
                    <ChevronRight size={16} className="text-gray-400" />
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>

        {/* Rules panel */}
        <div className="lg:col-span-2">
          {!selectedPayer ? (
            <div className="rounded-lg border border-gray-200 bg-white py-12 text-center text-sm text-gray-400" data-testid="no-payer-selected">
              Select a payer to view and manage rules
            </div>
          ) : (
            <div>
              <div className="mb-4 flex items-center justify-between">
                <h3 className="text-sm font-semibold text-gray-900">
                  Rules for {selectedPayer.name}
                </h3>
                <Button
                  size="sm"
                  onClick={() => {
                    setShowNewRule(true);
                    setEditingRule(null);
                  }}
                  data-testid="add-rule-button"
                >
                  <Plus size={14} />
                  Add Rule
                </Button>
              </div>

              {saveSuccess && (
                <div className="mb-4 rounded-md border border-green-200 bg-green-50 p-3 text-sm text-green-700" data-testid="save-success">
                  {saveSuccess}
                </div>
              )}
              {saveError && (
                <div className="mb-4 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-700" data-testid="save-error">
                  {saveError}
                </div>
              )}

              {/* New rule form */}
              {showNewRule && (
                <div className="mb-4 rounded-lg border border-teal-200 bg-teal-50 p-4" data-testid="new-rule-form">
                  <h4 className="mb-3 text-sm font-semibold text-gray-900">
                    New Rule
                  </h4>
                  <div className="grid grid-cols-2 gap-3">
                    <div>
                      <label className="mb-1 block text-xs font-medium text-gray-600">
                        Agent Type
                      </label>
                      <select
                        value={newRuleAgent}
                        onChange={(e) =>
                          setNewRuleAgent(e.target.value as AgentType)
                        }
                        className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm"
                      >
                        {AGENT_TYPES.map((t) => (
                          <option key={t} value={t}>
                            {AGENT_LABELS[t]}
                          </option>
                        ))}
                      </select>
                    </div>
                    <div>
                      <label className="mb-1 block text-xs font-medium text-gray-600">
                        Rule Type
                      </label>
                      <input
                        type="text"
                        value={newRuleType}
                        onChange={(e) => setNewRuleType(e.target.value)}
                        placeholder="e.g. pa_required"
                        className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm"
                      />
                    </div>
                    <div className="col-span-2">
                      <label className="mb-1 block text-xs font-medium text-gray-600">
                        Description
                      </label>
                      <input
                        type="text"
                        value={newRuleDesc}
                        onChange={(e) => setNewRuleDesc(e.target.value)}
                        className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm"
                      />
                    </div>
                    <div>
                      <label className="mb-1 block text-xs font-medium text-gray-600">
                        Effective Date
                      </label>
                      <input
                        type="date"
                        value={newRuleDate}
                        onChange={(e) => setNewRuleDate(e.target.value)}
                        className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm"
                      />
                    </div>
                    <div className="col-span-2">
                      <label className="mb-1 block text-xs font-medium text-gray-600">
                        Conditions (JSON)
                      </label>
                      <textarea
                        value={newRuleJson}
                        onChange={(e) => setNewRuleJson(e.target.value)}
                        rows={4}
                        className="w-full rounded-md border border-gray-300 px-3 py-2 font-mono text-xs"
                      />
                    </div>
                  </div>
                  <div className="mt-3 flex gap-2">
                    <Button
                      size="sm"
                      onClick={handleCreateRule}
                      disabled={saving || !newRuleType}
                    >
                      {saving ? (
                        <Loader2 size={14} className="animate-spin" />
                      ) : (
                        <Save size={14} />
                      )}
                      Create
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => setShowNewRule(false)}
                    >
                      Cancel
                    </Button>
                  </div>
                </div>
              )}

              {/* Rules error */}
              {rulesError && (
                <div className="mb-4 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-700" data-testid="rules-error">
                  {rulesError}
                </div>
              )}

              {/* Rule list */}
              {rulesLoading ? (
                <div className="flex items-center justify-center py-8">
                  <div className="h-5 w-5 animate-spin rounded-full border-4 border-teal-600 border-t-transparent" />
                </div>
              ) : rules.length === 0 ? (
                <div className="rounded-lg border border-gray-200 bg-white py-8 text-center text-sm text-gray-400" data-testid="rules-empty">
                  No rules configured for this payer
                </div>
              ) : (
                <div className="space-y-3" data-testid="rules-list">
                  {rules.map((rule) => (
                    <div
                      key={rule.id}
                      className="rounded-lg border border-gray-200 bg-white p-4"
                      data-testid={`rule-item-${rule.id}`}
                    >
                      {editingRule?.id === rule.id ? (
                        /* Edit mode */
                        <div>
                          <div className="mb-3">
                            <label className="mb-1 block text-xs font-medium text-gray-600">
                              Description
                            </label>
                            <input
                              type="text"
                              value={editDescription}
                              onChange={(e) =>
                                setEditDescription(e.target.value)
                              }
                              className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm"
                              data-testid="edit-description"
                            />
                          </div>
                          <div className="mb-3">
                            <label className="mb-1 block text-xs font-medium text-gray-600">
                              Conditions (JSON)
                            </label>
                            <textarea
                              value={editJson}
                              onChange={(e) => setEditJson(e.target.value)}
                              rows={6}
                              className="w-full rounded-md border border-gray-300 px-3 py-2 font-mono text-xs"
                              data-testid="edit-conditions"
                            />
                          </div>
                          <div className="flex gap-2">
                            <Button
                              size="sm"
                              onClick={handleSave}
                              disabled={saving}
                              data-testid="save-rule-button"
                            >
                              {saving ? (
                                <Loader2
                                  size={14}
                                  className="animate-spin"
                                />
                              ) : (
                                <Save size={14} />
                              )}
                              Save
                            </Button>
                            <Button
                              size="sm"
                              variant="outline"
                              onClick={() => setEditingRule(null)}
                            >
                              <X size={14} />
                              Cancel
                            </Button>
                          </div>
                        </div>
                      ) : (
                        /* View mode */
                        <div>
                          <div className="flex items-start justify-between">
                            <div>
                              <div className="flex items-center gap-2">
                                <span className="rounded bg-gray-100 px-2 py-0.5 text-xs font-medium text-gray-700">
                                  {rule.rule_type}
                                </span>
                                <span className="text-xs text-gray-500">
                                  {AGENT_LABELS[rule.agent_type] ??
                                    rule.agent_type}
                                </span>
                                <span
                                  className={`rounded-full px-2 py-0.5 text-xs font-medium ${
                                    rule.is_active
                                      ? "bg-green-100 text-green-800"
                                      : "bg-gray-100 text-gray-600"
                                  }`}
                                >
                                  {rule.is_active ? "Active" : "Inactive"}
                                </span>
                              </div>
                              {rule.description && (
                                <p className="mt-1 text-sm text-gray-700">
                                  {rule.description}
                                </p>
                              )}
                              <p className="mt-1 text-xs text-gray-500">
                                Effective: {rule.effective_date} | v
                                {rule.version}
                              </p>
                            </div>
                            <Button
                              size="sm"
                              variant="ghost"
                              onClick={() => startEdit(rule)}
                              data-testid={`edit-rule-${rule.id}`}
                            >
                              Edit
                            </Button>
                          </div>
                          <pre className="mt-2 max-h-32 overflow-auto rounded-md bg-gray-50 p-2 text-xs text-gray-600">
                            {JSON.stringify(rule.conditions, null, 2)}
                          </pre>
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
