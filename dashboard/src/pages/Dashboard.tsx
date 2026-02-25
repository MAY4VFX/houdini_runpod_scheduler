import { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { api, type Project } from '@/lib/api';

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
      {subtitle && <p className="text-sm text-gray-500 mt-1">{subtitle}</p>}
    </div>
  );
}

function Dashboard() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api.getProjects()
      .then((data) => setProjects(data.projects))
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  return (
    <div>
      <div className="mb-8">
        <h1 className="text-2xl font-bold text-gray-100">Dashboard</h1>
        <p className="text-gray-400 mt-1">Overview of your rendering infrastructure</p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6 mb-8">
        <StatCard title="Projects" value={loading ? '...' : projects.length} subtitle="total" />
        <StatCard title="Active Pods" value="--" subtitle="connect Redis to see" />
        <StatCard title="Active Jobs" value="--" subtitle="connect Redis to see" />
        <StatCard title="Status" value={loading ? '...' : 'Ready'} />
      </div>

      <div className="card p-0">
        <div className="px-6 py-4 border-b border-gray-700 flex items-center justify-between">
          <h2 className="text-lg font-semibold text-gray-100">Projects</h2>
          <Link to="/projects" className="text-indigo-400 hover:text-indigo-300 text-sm">View all</Link>
        </div>

        {loading ? (
          <div className="px-6 py-8 text-center text-gray-400">Loading...</div>
        ) : projects.length === 0 ? (
          <div className="px-6 py-12 text-center">
            <p className="text-gray-400 mb-4">No projects yet</p>
            <Link to="/projects" className="btn-primary inline-block">Create your first project</Link>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead className="bg-gray-800/50">
                <tr>
                  <th className="table-header">Name</th>
                  <th className="table-header">Bucket</th>
                  <th className="table-header">Created</th>
                  <th className="table-header"></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-700/50">
                {projects.map((project) => (
                  <tr key={project.id} className="hover:bg-gray-800/30 transition-colors">
                    <td className="table-cell font-medium text-gray-200">{project.name}</td>
                    <td className="table-cell text-gray-400">{project.b2Bucket}</td>
                    <td className="table-cell text-gray-400">
                      {new Date(project.createdAt).toLocaleDateString()}
                    </td>
                    <td className="table-cell">
                      <Link to={`/projects/${project.id}`}
                        className="text-indigo-400 hover:text-indigo-300 text-sm font-medium">
                        Manage
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

export default Dashboard;
