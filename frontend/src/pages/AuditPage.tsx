import { ScrollText } from "lucide-react";
import AuditLogViewer from "@/components/audit/AuditLogViewer";

export default function AuditPage() {
  return (
    <div>
      <div className="mb-6">
        <h1 className="flex items-center gap-2 font-display text-xl font-semibold text-slate-100">
          <ScrollText size={22} />
          Audit Log
        </h1>
        <p className="mt-1 text-sm text-slate-400">
          Immutable audit trail of all platform actions and PHI access.
        </p>
      </div>
      <AuditLogViewer />
    </div>
  );
}
