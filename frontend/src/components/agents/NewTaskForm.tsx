import { useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { createTask } from "@/api/agents";
import type { AgentType } from "@/types";
import { AGENT_LABELS } from "@/types";

interface NewTaskFormProps {
  agentType: AgentType;
  onCreated: () => void;
  onCancel: () => void;
}

/** Field definitions per agent type — drives the dynamic form. */
interface FieldDef {
  name: string;
  label: string;
  required: boolean;
  type: "text" | "date" | "textarea";
  placeholder?: string;
}

const AGENT_FIELDS: Record<AgentType, FieldDef[]> = {
  eligibility: [
    { name: "subscriber_id", label: "Subscriber ID", required: true, type: "text", placeholder: "e.g. MEM123456" },
    { name: "subscriber_first_name", label: "First Name", required: true, type: "text" },
    { name: "subscriber_last_name", label: "Last Name", required: true, type: "text" },
    { name: "subscriber_dob", label: "Date of Birth", required: false, type: "date" },
    { name: "payer_name", label: "Payer Name", required: false, type: "text" },
    { name: "provider_npi", label: "Provider NPI", required: false, type: "text", placeholder: "10-digit NPI" },
    { name: "date_of_service", label: "Date of Service", required: false, type: "date" },
  ],
  scheduling: [
    { name: "request_text", label: "Scheduling Request", required: true, type: "textarea", placeholder: "e.g. Annual checkup with Dr. Smith next Tuesday" },
    { name: "patient_first_name", label: "Patient First Name", required: false, type: "text" },
    { name: "patient_last_name", label: "Patient Last Name", required: false, type: "text" },
    { name: "provider_name", label: "Provider Name", required: false, type: "text" },
    { name: "specialty", label: "Specialty", required: false, type: "text" },
    { name: "preferred_date_start", label: "Preferred Start Date", required: false, type: "date" },
    { name: "preferred_date_end", label: "Preferred End Date", required: false, type: "date" },
  ],
  claims: [
    { name: "subscriber_id", label: "Subscriber ID", required: true, type: "text" },
    { name: "subscriber_first_name", label: "First Name", required: true, type: "text" },
    { name: "subscriber_last_name", label: "Last Name", required: true, type: "text" },
    { name: "diagnosis_codes", label: "Diagnosis Codes (comma-separated)", required: true, type: "text", placeholder: "e.g. J06.9, Z23" },
    { name: "procedure_codes", label: "Procedure Codes (comma-separated)", required: false, type: "text", placeholder: "e.g. 99213, 90471" },
    { name: "total_charge", label: "Total Charge ($)", required: false, type: "text", placeholder: "e.g. 150.00" },
    { name: "date_of_service", label: "Date of Service", required: false, type: "date" },
  ],
  prior_auth: [
    { name: "procedure_code", label: "Procedure Code", required: true, type: "text", placeholder: "e.g. 27447" },
    { name: "procedure_description", label: "Procedure Description", required: false, type: "text" },
    { name: "subscriber_id", label: "Subscriber ID", required: true, type: "text" },
    { name: "subscriber_first_name", label: "First Name", required: true, type: "text" },
    { name: "subscriber_last_name", label: "Last Name", required: true, type: "text" },
    { name: "payer_id", label: "Payer ID", required: true, type: "text" },
    { name: "patient_id", label: "Patient ID", required: true, type: "text" },
    { name: "date_of_service", label: "Date of Service", required: false, type: "date" },
  ],
  credentialing: [
    { name: "provider_npi", label: "Provider NPI", required: true, type: "text", placeholder: "10-digit NPI" },
    { name: "target_organization", label: "Target Organization", required: false, type: "text" },
    { name: "target_payer_id", label: "Target Payer ID", required: false, type: "text" },
    { name: "credentialing_type", label: "Credentialing Type", required: false, type: "text", placeholder: "e.g. initial, recredentialing" },
    { name: "state", label: "State", required: false, type: "text", placeholder: "e.g. CA" },
  ],
  compliance: [
    { name: "organization_id", label: "Organization ID", required: true, type: "text" },
    { name: "measure_set", label: "Measure Set", required: false, type: "text", placeholder: "e.g. HEDIS, MIPS, CMS_STARS" },
    { name: "reporting_period_start", label: "Reporting Period Start", required: true, type: "date" },
    { name: "reporting_period_end", label: "Reporting Period End", required: true, type: "date" },
  ],
};

/** Fields that should be split on comma into arrays when submitting. */
const ARRAY_FIELDS = new Set(["diagnosis_codes", "procedure_codes", "measure_ids"]);

export default function NewTaskForm({
  agentType,
  onCreated,
  onCancel,
}: NewTaskFormProps) {
  const fields = AGENT_FIELDS[agentType] ?? [];
  const [values, setValues] = useState<Record<string, string>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  function validate(): boolean {
    const newErrors: Record<string, string> = {};
    for (const field of fields) {
      if (field.required && !values[field.name]?.trim()) {
        newErrors[field.name] = `${field.label} is required`;
      }
    }
    setErrors(newErrors);
    return Object.keys(newErrors).length === 0;
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!validate()) return;

    setSubmitting(true);
    setSubmitError(null);
    try {
      const inputData: Record<string, unknown> = {};
      for (const [key, val] of Object.entries(values)) {
        if (!val.trim()) continue;
        if (ARRAY_FIELDS.has(key)) {
          inputData[key] = val.split(",").map((s) => s.trim()).filter(Boolean);
        } else {
          inputData[key] = val;
        }
      }
      await createTask(agentType, { input_data: inputData });
      onCreated();
    } catch (err) {
      setSubmitError(
        err instanceof Error ? err.message : "Failed to create task",
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} data-testid="new-task-form">
      <div className="rounded-lg border border-gray-200 bg-white p-5">
        <h3 className="mb-4 text-lg font-semibold text-gray-900">
          New {AGENT_LABELS[agentType]} Task
        </h3>

        {submitError && (
          <div
            className="mb-4 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-700"
            data-testid="submit-error"
          >
            {submitError}
          </div>
        )}

        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          {fields.map((field) => (
            <div
              key={field.name}
              className={field.type === "textarea" ? "sm:col-span-2" : ""}
            >
              <label className="mb-1 block text-sm font-medium text-gray-700">
                {field.label}
                {field.required && (
                  <span className="ml-0.5 text-red-500">*</span>
                )}
              </label>
              {field.type === "textarea" ? (
                <textarea
                  value={values[field.name] ?? ""}
                  onChange={(e) =>
                    setValues((prev) => ({
                      ...prev,
                      [field.name]: e.target.value,
                    }))
                  }
                  placeholder={field.placeholder}
                  rows={3}
                  className={`w-full rounded-md border px-3 py-2 text-sm focus:outline-none focus:ring-1 ${
                    errors[field.name]
                      ? "border-red-300 focus:border-red-500 focus:ring-red-500"
                      : "border-gray-300 focus:border-teal-500 focus:ring-teal-500"
                  }`}
                  data-testid={`field-${field.name}`}
                />
              ) : (
                <input
                  type={field.type}
                  value={values[field.name] ?? ""}
                  onChange={(e) =>
                    setValues((prev) => ({
                      ...prev,
                      [field.name]: e.target.value,
                    }))
                  }
                  placeholder={field.placeholder}
                  className={`w-full rounded-md border px-3 py-2 text-sm focus:outline-none focus:ring-1 ${
                    errors[field.name]
                      ? "border-red-300 focus:border-red-500 focus:ring-red-500"
                      : "border-gray-300 focus:border-teal-500 focus:ring-teal-500"
                  }`}
                  data-testid={`field-${field.name}`}
                />
              )}
              {errors[field.name] && (
                <p
                  className="mt-1 text-xs text-red-600"
                  data-testid={`error-${field.name}`}
                >
                  {errors[field.name]}
                </p>
              )}
            </div>
          ))}
        </div>

        <div className="mt-6 flex gap-3">
          <Button type="submit" disabled={submitting}>
            {submitting ? "Creating..." : "Create Task"}
          </Button>
          <Button
            type="button"
            variant="outline"
            onClick={onCancel}
            disabled={submitting}
          >
            Cancel
          </Button>
        </div>
      </div>
    </form>
  );
}
