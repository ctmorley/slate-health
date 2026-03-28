import { ScrollText } from "lucide-react";
import AuditLogViewer from "@/components/audit/AuditLogViewer";

export default function AuditPage() {
  return (
    <div>
      <div className="mb-6">
        <h1 className="flex items-center gap-2 text-xl font-semibold text-gray-900">
          <ScrollText size={22} />
          Audit Log
        </h1>
        <p className="mt-1 text-sm text-gray-500">
          Immutable audit trail of all platform actions and PHI access.
        </p>
      </div>
      <AuditLogViewer />
    </div>
  );
}
