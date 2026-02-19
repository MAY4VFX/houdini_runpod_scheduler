import type { RenderJob } from '@/lib/api';

// TODO: Replace with real API data via SWR
const placeholderStats = {
  activeJobs: 12,
  activePods: 4,
  currentCost: 23.47,
  completedToday: 87,
};

// TODO: Replace with real API data
const placeholderJobs: RenderJob[] = [
  {
    id: '1',
    name: 'shot_010_lighting_v3',
    status: 'running',
    progress: 67,
    podId: 'pod-a1b2c3',
    startedAt: '2026-02-19T10:30:00Z',
    completedAt: null,
    cost: 2.34,
  },
  {
    id: '2',
    name: 'shot_020_fx_sim_v1',
    status: 'running',
    progress: 23,
    podId: 'pod-d4e5f6',
    startedAt: '2026-02-19T11:15:00Z',
    completedAt: null,
    cost: 1.12,
  },
  {
    id: '3',
    name: 'shot_005_comp_v2',
    status: 'completed',
    progress: 100,
    podId: null,
    startedAt: '2026-02-19T08:00:00Z',
    completedAt: '2026-02-19T09:45:00Z',
    cost: 3.56,
  },
  {
    id: '4',
    name: 'env_forest_scatter_v1',
    status: 'queued',
    progress: 0,
    podId: null,
    startedAt: null,
    completedAt: null,
    cost: 0,
  },
  {
    id: '5',
    name: 'shot_015_hair_sim_v4',
    status: 'failed',
    progress: 45,
    podId: null,
    startedAt: '2026-02-19T07:00:00Z',
    completedAt: '2026-02-19T07:32:00Z',
    cost: 0.89,
  },
];

function StatusBadge({ status }: { status: RenderJob['status'] }) {
  const styles: Record<RenderJob['status'], string> = {
    queued: 'bg-gray-600 text-gray-200',
    running: 'bg-blue-900/50 text-blue-300',
    completed: 'bg-green-900/50 text-green-300',
    failed: 'bg-red-900/50 text-red-300',
    cancelled: 'bg-yellow-900/50 text-yellow-300',
  };

  return (
    <span
      className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${styles[status]}`}
    >
      {status}
    </span>
  );
}

function StatCard({
  title,
  value,
  subtitle,
}: {
  title: string;
  value: string | number;
  subtitle?: string;
}) {
  return (
    <div className="card">
      <p className="text-sm font-medium text-gray-400">{title}</p>
      <p className="text-3xl font-bold text-gray-100 mt-2">{value}</p>
      {subtitle && (
        <p className="text-sm text-gray-500 mt-1">{subtitle}</p>
      )}
    </div>
  );
}

function Dashboard() {
  return (
    <div>
      <div className="mb-8">
        <h1 className="text-2xl font-bold text-gray-100">Dashboard</h1>
        <p className="text-gray-400 mt-1">
          Overview of your rendering infrastructure
        </p>
      </div>

      {/* Stats Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6 mb-8">
        <StatCard
          title="Active Render Jobs"
          value={placeholderStats.activeJobs}
          subtitle="across 3 projects"
        />
        <StatCard
          title="Active Pods"
          value={placeholderStats.activePods}
          subtitle="2x A100, 2x RTX 4090"
        />
        <StatCard
          title="Current Session Cost"
          value={`$${placeholderStats.currentCost.toFixed(2)}`}
          subtitle="since 08:00 UTC"
        />
        <StatCard
          title="Completed Today"
          value={placeholderStats.completedToday}
          subtitle="avg 4.2 min/job"
        />
      </div>

      {/* Recent Jobs Table */}
      <div className="card p-0">
        <div className="px-6 py-4 border-b border-gray-700">
          <h2 className="text-lg font-semibold text-gray-100">Recent Jobs</h2>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead className="bg-gray-800/50">
              <tr>
                <th className="table-header">Name</th>
                <th className="table-header">Status</th>
                <th className="table-header">Progress</th>
                <th className="table-header">Pod</th>
                <th className="table-header">Cost</th>
                <th className="table-header">Started</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-700/50">
              {placeholderJobs.map((job) => (
                <tr
                  key={job.id}
                  className="hover:bg-gray-800/30 transition-colors"
                >
                  <td className="table-cell font-medium text-gray-200">
                    {job.name}
                  </td>
                  <td className="table-cell">
                    <StatusBadge status={job.status} />
                  </td>
                  <td className="table-cell">
                    <div className="flex items-center gap-3">
                      <div className="w-24 h-1.5 bg-gray-700 rounded-full overflow-hidden">
                        <div
                          className={`h-full rounded-full ${
                            job.status === 'failed'
                              ? 'bg-red-500'
                              : job.status === 'completed'
                                ? 'bg-green-500'
                                : 'bg-indigo-500'
                          }`}
                          style={{ width: `${job.progress}%` }}
                        />
                      </div>
                      <span className="text-xs text-gray-400">
                        {job.progress}%
                      </span>
                    </div>
                  </td>
                  <td className="table-cell">
                    {job.podId ? (
                      <code className="text-xs bg-gray-700 px-2 py-1 rounded">
                        {job.podId}
                      </code>
                    ) : (
                      <span className="text-gray-500">--</span>
                    )}
                  </td>
                  <td className="table-cell">
                    ${job.cost.toFixed(2)}
                  </td>
                  <td className="table-cell text-gray-400">
                    {job.startedAt
                      ? new Date(job.startedAt).toLocaleTimeString()
                      : '--'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

export default Dashboard;
