import { useState, useEffect, useCallback } from 'react';
import { useParams, Link } from 'react-router-dom';
import { api, type Project, type Artist, type ArtistWithKey } from '@/lib/api';

type TabId = 'artists' | 'monitoring' | 'settings';

const tabs: { id: TabId; label: string }[] = [
  { id: 'artists', label: 'Artists & API Keys' },
  { id: 'monitoring', label: 'Monitoring' },
  { id: 'settings', label: 'Settings' },
];

function ArtistsTab({ projectId }: { projectId: string }) {
  const [artists, setArtists] = useState<Artist[]>([]);
  const [loading, setLoading] = useState(true);
  const [showAdd, setShowAdd] = useState(false);
  const [newName, setNewName] = useState('');
  const [newEmail, setNewEmail] = useState('');
  const [addError, setAddError] = useState<string | null>(null);
  const [addLoading, setAddLoading] = useState(false);
  const [createdKey, setCreatedKey] = useState<ArtistWithKey | null>(null);
  const [copied, setCopied] = useState(false);

  const fetchArtists = useCallback(async () => {
    try {
      const data = await api.getArtists(projectId);
      setArtists(data.artists);
    } catch {
      // ignore
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    fetchArtists();
  }, [fetchArtists]);

  const handleAddArtist = async (e: React.FormEvent) => {
    e.preventDefault();
    setAddLoading(true);
    setAddError(null);
    try {
      const data = await api.addArtist(projectId, { name: newName, email: newEmail });
      setCreatedKey(data.artist);
      setNewName('');
      setNewEmail('');
      setShowAdd(false);
      fetchArtists();
    } catch (err) {
      setAddError(err instanceof Error ? err.message : 'Failed to add artist');
    } finally {
      setAddLoading(false);
    }
  };

  const handleRevoke = async (artistId: string) => {
    if (!confirm('Revoke this artist\'s access? They will no longer be able to connect.')) return;
    try {
      await api.revokeArtist(projectId, artistId);
      fetchArtists();
    } catch {
      // ignore
    }
  };

  const copyKey = (key: string) => {
    navigator.clipboard.writeText(key);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  if (loading) return <div className="text-gray-400 py-8">Loading artists...</div>;

  return (
    <div>
      {/* Created API key banner */}
      {createdKey && (
        <div className="mb-6 rounded-lg border border-green-800/50 bg-green-900/20 px-6 py-4">
          <p className="text-green-300 font-medium mb-2">
            Artist "{createdKey.name}" created! Copy the API key now — it won't be shown again.
          </p>
          <div className="flex items-center gap-3">
            <code className="flex-1 bg-black/40 text-green-200 px-4 py-2 rounded font-mono text-sm select-all">
              {createdKey.apiKey}
            </code>
            <button onClick={() => copyKey(createdKey.apiKey)}
              className="btn-primary text-sm px-4 py-2">
              {copied ? 'Copied!' : 'Copy'}
            </button>
            <button onClick={() => setCreatedKey(null)}
              className="btn-secondary text-sm px-4 py-2">
              Dismiss
            </button>
          </div>
        </div>
      )}

      <div className="flex justify-end mb-4">
        <button onClick={() => setShowAdd(!showAdd)} className="btn-primary">Add Artist</button>
      </div>

      {showAdd && (
        <div className="card mb-6">
          <form onSubmit={handleAddArtist} className="flex items-end gap-4">
            <div className="flex-1">
              <label className="block text-sm font-medium text-gray-300 mb-1.5">Name</label>
              <input value={newName} onChange={(e) => setNewName(e.target.value)}
                className="input-field" placeholder="John Smith" required />
            </div>
            <div className="flex-1">
              <label className="block text-sm font-medium text-gray-300 mb-1.5">Email</label>
              <input type="email" value={newEmail} onChange={(e) => setNewEmail(e.target.value)}
                className="input-field" placeholder="john@studio.com" required />
            </div>
            <button type="submit" className="btn-primary" disabled={addLoading}>
              {addLoading ? 'Adding...' : 'Add'}
            </button>
            <button type="button" onClick={() => setShowAdd(false)} className="btn-secondary">Cancel</button>
          </form>
          {addError && (
            <p className="text-red-400 text-sm mt-2">{addError}</p>
          )}
        </div>
      )}

      {artists.length === 0 ? (
        <div className="text-center py-12 text-gray-400">
          <p className="mb-4">No artists yet. Add an artist to generate an API key for the Desktop App.</p>
        </div>
      ) : (
        <div className="card p-0">
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead className="bg-gray-800/50">
                <tr>
                  <th className="table-header">Name</th>
                  <th className="table-header">Email</th>
                  <th className="table-header">Status</th>
                  <th className="table-header">Added</th>
                  <th className="table-header">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-700/50">
                {artists.map((artist) => (
                  <tr key={artist.id} className="hover:bg-gray-800/30 transition-colors">
                    <td className="table-cell font-medium text-gray-200">{artist.name}</td>
                    <td className="table-cell text-gray-400">{artist.email}</td>
                    <td className="table-cell">
                      {artist.revokedAt ? (
                        <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-red-900/50 text-red-300">
                          revoked
                        </span>
                      ) : (
                        <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-green-900/50 text-green-300">
                          active
                        </span>
                      )}
                    </td>
                    <td className="table-cell text-gray-400">
                      {new Date(artist.createdAt).toLocaleDateString()}
                    </td>
                    <td className="table-cell">
                      {!artist.revokedAt && (
                        <button onClick={() => handleRevoke(artist.id)}
                          className="text-red-400 hover:text-red-300 text-sm font-medium transition-colors">
                          Revoke
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

function MonitoringTab({ projectId }: { projectId: string }) {
  const [jobs, setJobs] = useState<{ id: string; status: string }[]>([]);
  const [pods, setPods] = useState<{ id: string; alive: boolean }[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function fetch() {
      try {
        const [jobsData, podsData] = await Promise.all([
          api.getMonitoringJobs(projectId).catch(() => ({ jobs: [], pendingQueues: 0 })),
          api.getMonitoringPods(projectId).catch(() => ({ pods: [], totalHeartbeats: 0 })),
        ]);
        setJobs(jobsData.jobs);
        setPods(podsData.pods);
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to load monitoring data');
      } finally {
        setLoading(false);
      }
    }
    fetch();
  }, [projectId]);

  if (loading) return <div className="text-gray-400 py-8">Loading monitoring data...</div>;
  if (error) return <div className="text-red-400 py-8">{error}</div>;

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        <div className="card">
          <p className="text-sm font-medium text-gray-400">Active Jobs</p>
          <p className="text-3xl font-bold text-gray-100 mt-2">{jobs.length}</p>
        </div>
        <div className="card">
          <p className="text-sm font-medium text-gray-400">Active Pods</p>
          <p className="text-3xl font-bold text-gray-100 mt-2">{pods.filter(p => p.alive).length}</p>
          <p className="text-sm text-gray-500 mt-1">of {pods.length} registered</p>
        </div>
        <div className="card">
          <p className="text-sm font-medium text-gray-400">Status</p>
          <p className="text-xl font-bold text-gray-100 mt-2">
            {jobs.length > 0 || pods.length > 0 ? 'Active' : 'Idle'}
          </p>
        </div>
      </div>

      {pods.length > 0 && (
        <div className="card p-0">
          <div className="px-6 py-4 border-b border-gray-700">
            <h3 className="text-lg font-semibold text-gray-100">Pods</h3>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead className="bg-gray-800/50">
                <tr>
                  <th className="table-header">Pod ID</th>
                  <th className="table-header">Status</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-700/50">
                {pods.map((pod) => (
                  <tr key={pod.id} className="hover:bg-gray-800/30">
                    <td className="table-cell font-mono text-sm text-gray-200">{pod.id}</td>
                    <td className="table-cell">
                      <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${
                        pod.alive ? 'bg-green-900/50 text-green-300' : 'bg-gray-600 text-gray-300'
                      }`}>
                        {pod.alive ? 'alive' : 'offline'}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {jobs.length === 0 && pods.length === 0 && (
        <div className="text-center py-12 text-gray-400">
          <p>No active jobs or pods. Start a render from Houdini to see monitoring data.</p>
        </div>
      )}
    </div>
  );
}

function SettingsTab({ project }: { project: Project }) {
  return (
    <div className="space-y-6">
      <div className="card">
        <h3 className="text-lg font-semibold text-gray-100 mb-4">Project Info</h3>
        <dl className="space-y-3">
          <div>
            <dt className="text-sm text-gray-400">Project ID</dt>
            <dd className="text-sm font-mono text-gray-200 mt-0.5">{project.id}</dd>
          </div>
          <div>
            <dt className="text-sm text-gray-400">B2 Bucket</dt>
            <dd className="text-sm text-gray-200 mt-0.5">{project.b2Bucket}</dd>
          </div>
          <div>
            <dt className="text-sm text-gray-400">B2 Endpoint</dt>
            <dd className="text-sm text-gray-200 mt-0.5">{project.b2Endpoint}</dd>
          </div>
          <div>
            <dt className="text-sm text-gray-400">Created</dt>
            <dd className="text-sm text-gray-200 mt-0.5">
              {new Date(project.createdAt).toLocaleString()}
            </dd>
          </div>
        </dl>
      </div>
    </div>
  );
}

function ProjectDetail() {
  const { id } = useParams<{ id: string }>();
  const [activeTab, setActiveTab] = useState<TabId>('artists');
  const [project, setProject] = useState<Project | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!id) return;
    api.getProjects().then((data) => {
      const found = data.projects.find((p) => p.id === id);
      setProject(found || null);
      setLoading(false);
    }).catch(() => setLoading(false));
  }, [id]);

  if (loading) return <div className="text-gray-400 py-12 text-center">Loading...</div>;
  if (!project) {
    return (
      <div className="text-center py-12">
        <p className="text-gray-400 mb-4">Project not found</p>
        <Link to="/projects" className="text-indigo-400 hover:text-indigo-300">Back to projects</Link>
      </div>
    );
  }

  return (
    <div>
      <div className="mb-8">
        <div className="flex items-center gap-3 mb-1">
          <Link to="/projects" className="text-gray-400 hover:text-gray-300">&larr;</Link>
          <h1 className="text-2xl font-bold text-gray-100">{project.name}</h1>
        </div>
        <p className="text-gray-400 text-sm ml-8">{project.b2Bucket} &middot; {project.b2Endpoint}</p>
      </div>

      <div className="border-b border-gray-700 mb-6">
        <nav className="flex gap-0">
          {tabs.map((tab) => (
            <button key={tab.id} onClick={() => setActiveTab(tab.id)}
              className={`px-5 py-3 text-sm font-medium border-b-2 transition-colors ${
                activeTab === tab.id
                  ? 'border-indigo-500 text-indigo-400'
                  : 'border-transparent text-gray-400 hover:text-gray-300 hover:border-gray-600'
              }`}>
              {tab.label}
            </button>
          ))}
        </nav>
      </div>

      {activeTab === 'artists' && <ArtistsTab projectId={project.id} />}
      {activeTab === 'monitoring' && <MonitoringTab projectId={project.id} />}
      {activeTab === 'settings' && <SettingsTab project={project} />}
    </div>
  );
}

export default ProjectDetail;
