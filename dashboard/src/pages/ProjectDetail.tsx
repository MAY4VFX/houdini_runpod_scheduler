import { useState } from 'react';
import { useParams } from 'react-router-dom';
import type { RenderJob, Pod, Artist } from '@/lib/api';

type TabId = 'overview' | 'jobs' | 'pods' | 'artists';

// TODO: Replace with real API data via SWR using useParams().id
const placeholderProject = {
  id: '1',
  name: 'Feature Film - The Forest',
  slug: 'the-forest',
  status: 'active' as const,
  createdAt: '2026-01-15T10:00:00Z',
  updatedAt: '2026-02-19T08:30:00Z',
};

const placeholderJobs: RenderJob[] = [
  {
    id: 'j1',
    name: 'shot_010_lighting_v3',
    status: 'running',
    progress: 67,
    podId: 'pod-a1b2c3',
    startedAt: '2026-02-19T10:30:00Z',
    completedAt: null,
    cost: 2.34,
  },
  {
    id: 'j2',
    name: 'shot_020_fx_sim_v1',
    status: 'completed',
    progress: 100,
    podId: null,
    startedAt: '2026-02-19T08:00:00Z',
    completedAt: '2026-02-19T09:45:00Z',
    cost: 3.56,
  },
  {
    id: 'j3',
    name: 'shot_005_comp_v2',
    status: 'queued',
    progress: 0,
    podId: null,
    startedAt: null,
    completedAt: null,
    cost: 0,
  },
];

const placeholderPods: Pod[] = [
  {
    id: 'pod-a1b2c3',
    name: 'render-node-01',
    gpuType: 'NVIDIA A100 80GB',
    status: 'running',
    costPerHour: 1.64,
    uptimeHours: 3.5,
  },
  {
    id: 'pod-d4e5f6',
    name: 'render-node-02',
    gpuType: 'NVIDIA RTX 4090',
    status: 'idle',
    costPerHour: 0.74,
    uptimeHours: 6.2,
  },
  {
    id: 'pod-g7h8i9',
    name: 'render-node-03',
    gpuType: 'NVIDIA A100 80GB',
    status: 'starting',
    costPerHour: 1.64,
    uptimeHours: 0,
  },
];

const placeholderArtists: Artist[] = [
  {
    id: 'a1',
    email: 'john@studio.com',
    name: 'John Smith',
    role: 'lead',
    addedAt: '2026-01-15T10:00:00Z',
  },
  {
    id: 'a2',
    email: 'jane@studio.com',
    name: 'Jane Doe',
    role: 'artist',
    addedAt: '2026-01-20T14:00:00Z',
  },
  {
    id: 'a3',
    email: 'mike@studio.com',
    name: 'Mike Chen',
    role: 'artist',
    addedAt: '2026-02-05T09:00:00Z',
  },
];

const tabs: { id: TabId; label: string }[] = [
  { id: 'overview', label: 'Overview' },
  { id: 'jobs', label: 'Render Jobs' },
  { id: 'pods', label: 'Pods' },
  { id: 'artists', label: 'Artists' },
];

function JobStatusBadge({ status }: { status: RenderJob['status'] }) {
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

function PodStatusBadge({ status }: { status: Pod['status'] }) {
  const styles: Record<Pod['status'], string> = {
    running: 'bg-green-900/50 text-green-300',
    idle: 'bg-yellow-900/50 text-yellow-300',
    starting: 'bg-blue-900/50 text-blue-300',
    stopping: 'bg-orange-900/50 text-orange-300',
    terminated: 'bg-gray-600 text-gray-300',
  };
  return (
    <span
      className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${styles[status]}`}
    >
      {status}
    </span>
  );
}

function RoleBadge({ role }: { role: Artist['role'] }) {
  const styles: Record<Artist['role'], string> = {
    admin: 'bg-purple-900/50 text-purple-300',
    lead: 'bg-indigo-900/50 text-indigo-300',
    artist: 'bg-gray-600 text-gray-300',
  };
  return (
    <span
      className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${styles[role]}`}
    >
      {role}
    </span>
  );
}

function OverviewTab() {
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
      <div className="card">
        <p className="text-sm font-medium text-gray-400">Total Jobs</p>
        <p className="text-3xl font-bold text-gray-100 mt-2">
          {placeholderJobs.length}
        </p>
        <p className="text-sm text-gray-500 mt-1">
          {placeholderJobs.filter((j) => j.status === 'running').length} running
        </p>
      </div>
      <div className="card">
        <p className="text-sm font-medium text-gray-400">Active Pods</p>
        <p className="text-3xl font-bold text-gray-100 mt-2">
          {placeholderPods.filter((p) => p.status === 'running' || p.status === 'idle').length}
        </p>
        <p className="text-sm text-gray-500 mt-1">
          of {placeholderPods.length} total
        </p>
      </div>
      <div className="card">
        <p className="text-sm font-medium text-gray-400">Total Cost</p>
        <p className="text-3xl font-bold text-gray-100 mt-2">
          ${placeholderJobs.reduce((sum, j) => sum + j.cost, 0).toFixed(2)}
        </p>
        <p className="text-sm text-gray-500 mt-1">this session</p>
      </div>
      <div className="card">
        <p className="text-sm font-medium text-gray-400">Artists</p>
        <p className="text-3xl font-bold text-gray-100 mt-2">
          {placeholderArtists.length}
        </p>
        <p className="text-sm text-gray-500 mt-1">
          {placeholderArtists.filter((a) => a.role === 'lead').length} leads
        </p>
      </div>
    </div>
  );
}

function JobsTab() {
  return (
    <div className="card p-0">
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
                  <JobStatusBadge status={job.status} />
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
                <td className="table-cell">${job.cost.toFixed(2)}</td>
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
  );
}

function PodsTab() {
  return (
    <div className="card p-0">
      <div className="overflow-x-auto">
        <table className="w-full">
          <thead className="bg-gray-800/50">
            <tr>
              <th className="table-header">Name</th>
              <th className="table-header">GPU</th>
              <th className="table-header">Status</th>
              <th className="table-header">Cost/hr</th>
              <th className="table-header">Uptime</th>
              <th className="table-header">Total Cost</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-700/50">
            {placeholderPods.map((pod) => (
              <tr
                key={pod.id}
                className="hover:bg-gray-800/30 transition-colors"
              >
                <td className="table-cell">
                  <div>
                    <p className="font-medium text-gray-200">{pod.name}</p>
                    <p className="text-xs text-gray-500">{pod.id}</p>
                  </div>
                </td>
                <td className="table-cell text-gray-300">{pod.gpuType}</td>
                <td className="table-cell">
                  <PodStatusBadge status={pod.status} />
                </td>
                <td className="table-cell">${pod.costPerHour.toFixed(2)}</td>
                <td className="table-cell text-gray-400">
                  {pod.uptimeHours.toFixed(1)}h
                </td>
                <td className="table-cell">
                  ${(pod.costPerHour * pod.uptimeHours).toFixed(2)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function ArtistsTab() {
  const [showAdd, setShowAdd] = useState(false);
  const [newEmail, setNewEmail] = useState('');
  const [newRole, setNewRole] = useState<Artist['role']>('artist');

  const handleAddArtist = (e: React.FormEvent) => {
    e.preventDefault();
    // TODO: Call api.addArtist(projectId, { email: newEmail, role: newRole })
    console.log('Adding artist:', { email: newEmail, role: newRole });
    setNewEmail('');
    setNewRole('artist');
    setShowAdd(false);
  };

  const handleRevoke = (artistId: string) => {
    // TODO: Call api.revokeArtist(projectId, artistId)
    console.log('Revoking artist:', artistId);
  };

  return (
    <div>
      <div className="flex justify-end mb-4">
        <button onClick={() => setShowAdd(!showAdd)} className="btn-primary">
          Add Artist
        </button>
      </div>

      {showAdd && (
        <div className="card mb-6">
          <form
            onSubmit={handleAddArtist}
            className="flex items-end gap-4"
          >
            <div className="flex-1">
              <label
                htmlFor="artist-email"
                className="block text-sm font-medium text-gray-300 mb-1.5"
              >
                Email
              </label>
              <input
                id="artist-email"
                type="email"
                value={newEmail}
                onChange={(e) => setNewEmail(e.target.value)}
                className="input-field"
                placeholder="artist@studio.com"
                required
              />
            </div>
            <div className="w-40">
              <label
                htmlFor="artist-role"
                className="block text-sm font-medium text-gray-300 mb-1.5"
              >
                Role
              </label>
              <select
                id="artist-role"
                value={newRole}
                onChange={(e) => setNewRole(e.target.value as Artist['role'])}
                className="input-field"
              >
                <option value="artist">Artist</option>
                <option value="lead">Lead</option>
                <option value="admin">Admin</option>
              </select>
            </div>
            <button type="submit" className="btn-primary">
              Add
            </button>
            <button
              type="button"
              onClick={() => setShowAdd(false)}
              className="btn-secondary"
            >
              Cancel
            </button>
          </form>
        </div>
      )}

      <div className="card p-0">
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead className="bg-gray-800/50">
              <tr>
                <th className="table-header">Name</th>
                <th className="table-header">Email</th>
                <th className="table-header">Role</th>
                <th className="table-header">Added</th>
                <th className="table-header">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-700/50">
              {placeholderArtists.map((artist) => (
                <tr
                  key={artist.id}
                  className="hover:bg-gray-800/30 transition-colors"
                >
                  <td className="table-cell font-medium text-gray-200">
                    {artist.name}
                  </td>
                  <td className="table-cell text-gray-400">
                    {artist.email}
                  </td>
                  <td className="table-cell">
                    <RoleBadge role={artist.role} />
                  </td>
                  <td className="table-cell text-gray-400">
                    {new Date(artist.addedAt).toLocaleDateString()}
                  </td>
                  <td className="table-cell">
                    <button
                      onClick={() => handleRevoke(artist.id)}
                      className="text-red-400 hover:text-red-300 text-sm font-medium transition-colors"
                    >
                      Revoke
                    </button>
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

function ProjectDetail() {
  const { id } = useParams<{ id: string }>();
  const [activeTab, setActiveTab] = useState<TabId>('overview');

  // TODO: Fetch project data using id via SWR
  // const { data: project } = useSWR(`/projects/${id}`, () => api.getProject(id!));
  void id; // Used for future API integration
  const project = placeholderProject;

  return (
    <div>
      {/* Header */}
      <div className="mb-8">
        <div className="flex items-center gap-3 mb-1">
          <h1 className="text-2xl font-bold text-gray-100">{project.name}</h1>
          <span
            className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${
              project.status === 'active'
                ? 'bg-green-900/50 text-green-300'
                : 'bg-gray-600 text-gray-300'
            }`}
          >
            {project.status}
          </span>
        </div>
        <p className="text-gray-400">/{project.slug}</p>
      </div>

      {/* Tabs */}
      <div className="border-b border-gray-700 mb-6">
        <nav className="flex gap-0">
          {tabs.map((tab) => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={`px-5 py-3 text-sm font-medium border-b-2 transition-colors ${
                activeTab === tab.id
                  ? 'border-indigo-500 text-indigo-400'
                  : 'border-transparent text-gray-400 hover:text-gray-300 hover:border-gray-600'
              }`}
            >
              {tab.label}
            </button>
          ))}
        </nav>
      </div>

      {/* Tab Content */}
      {activeTab === 'overview' && <OverviewTab />}
      {activeTab === 'jobs' && <JobsTab />}
      {activeTab === 'pods' && <PodsTab />}
      {activeTab === 'artists' && <ArtistsTab />}
    </div>
  );
}

export default ProjectDetail;
