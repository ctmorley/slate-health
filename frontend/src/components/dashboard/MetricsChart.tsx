import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Legend,
} from "recharts";

interface DayData {
  date: string;
  count: number;
}

interface MetricsChartProps {
  /** Array of { date, count } objects, typically the last 7 days. */
  data: DayData[];
  title?: string;
  /**
   * Names of agents whose metrics failed to load.  When non-empty the chart
   * renders a subtle "incomplete data" indicator so users know the totals may
   * be understated — complementing the top-level warning banner.
   */
  incompleteAgents?: string[];
}

/**
 * Parse an ISO date string (YYYY-MM-DD) as a local date and format it.
 * Using `new Date("YYYY-MM-DD")` parses as UTC midnight, which shifts to the
 * previous day in negative-UTC timezones (e.g. America/New_York).  Splitting
 * the string and constructing via `new Date(y, m-1, d)` avoids this.
 */
export function formatDateLabel(iso: string): string {
  const [y, m, d] = iso.split("-").map(Number);
  const local = new Date(y, m - 1, d);
  return local.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

export default function MetricsChart({
  data,
  title = "Task Volume (Last 7 Days)",
  incompleteAgents = [],
}: MetricsChartProps) {
  const formatted = data.map((d) => ({
    ...d,
    label: formatDateLabel(d.date),
  }));

  const isPartial = incompleteAgents.length > 0;

  return (
    <div
      className={`rounded-lg border bg-white p-4 shadow-sm ${
        isPartial ? "border-amber-300" : "border-gray-200"
      }`}
      data-testid="metrics-chart"
    >
      <div className="mb-4 flex items-center justify-between">
        <h3 className="text-sm font-semibold text-gray-900">{title}</h3>
        {isPartial && (
          <span
            className="rounded-full bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-700"
            data-testid="chart-incomplete-badge"
            title={`Missing data from: ${incompleteAgents.join(", ")}`}
          >
            Partial data
          </span>
        )}
      </div>

      {formatted.length === 0 ? (
        <p className="py-8 text-center text-sm text-gray-400">
          No data available
        </p>
      ) : (
        <ResponsiveContainer width="100%" height={260}>
          <LineChart data={formatted}>
            <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />
            <XAxis
              dataKey="label"
              tick={{ fontSize: 12, fill: "#6b7280" }}
              axisLine={false}
              tickLine={false}
            />
            <YAxis
              tick={{ fontSize: 12, fill: "#6b7280" }}
              axisLine={false}
              tickLine={false}
              allowDecimals={false}
            />
            <Tooltip
              contentStyle={{
                borderRadius: 8,
                border: "1px solid #e5e7eb",
                fontSize: 13,
              }}
            />
            <Legend wrapperStyle={{ fontSize: 12 }} />
            <Line
              type="monotone"
              dataKey="count"
              name="Tasks"
              stroke="#0d9488"
              strokeWidth={2}
              dot={{ r: 3, fill: "#0d9488" }}
              activeDot={{ r: 5 }}
            />
          </LineChart>
        </ResponsiveContainer>
      )}
    </div>
  );
}
