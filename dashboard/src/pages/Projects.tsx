import { useState, useEffect, useCallback } from 'react';
import { Link } from 'react-router-dom';
import { api, type Project, type CreateProjectPayload } from '@/lib/api';

function CreateProjectModal({
  isOpen,
  onClose,
  onCreated,
}: {
  isOpen: boolean;
  onClose: () => void;
  onCreated: () => void;
}) {
  const [form, setForm] = useState<CreateProjectPayload>({
    name: '',
    redisUrl: '',
    b2Endpoint: '',
    b2AccessKey: '',
    b2SecretKey: '',
    b2Bucket: '',
    juicefsRsaKey: '',
  });
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  if (!isOpen) return null;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      await api.createProject(form);
      onCreated();
      onClose();
      setForm({ name: '', redisUrl: '', b2Endpoint: '', b2AccessKey: '', b2SecretKey: '', b2Bucket: '', juicefsRsaKey: '' });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create project');
    } finally {
      setLoading(false);
    }
  };

  const update = (field: keyof CreateProjectPayload, value: string) =>
    setForm((prev) => ({ ...prev, [field]: value }));

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50">
      <div className="card w-full max-w-lg mx-4 max-h-[90vh] overflow-y-auto">
        <h2 className="text-lg font-semibold text-gray-100 mb-4">Create Project</h2>
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-gray-300 mb-1.5">Project Name</label>
            <input value={form.name} onChange={(e) => update('name', e.target.value)}
              className="input-field" placeholder="My VFX Project" required autoFocus />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-300 mb-1.5">Redis URL</label>
            <input value={form.redisUrl} onChange={(e) => update('redisUrl', e.target.value)}
              className="input-field" placeholder="redis://:password@host:6379/0" required />
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-300 mb-1.5">B2 Endpoint</label>
              <input value={form.b2Endpoint} onChange={(e) => update('b2Endpoint', e.target.value)}
                className="input-field" placeholder="https://s3.eu-central-003.backblazeb2.com" required />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-300 mb-1.5">B2 Bucket</label>
              <input value={form.b2Bucket} onChange={(e) => update('b2Bucket', e.target.value)}
                className="input-field" placeholder="my-bucket" required />
            </div>
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-300 mb-1.5">B2 Access Key</label>
              <input value={form.b2AccessKey} onChange={(e) => update('b2AccessKey', e.target.value)}
                className="input-field" required />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-300 mb-1.5">B2 Secret Key</label>
              <input type="password" value={form.b2SecretKey} onChange={(e) => update('b2SecretKey', e.target.value)}
                className="input-field" required />
            </div>
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-300 mb-1.5">JuiceFS RSA Key</label>
            <textarea value={form.juicefsRsaKey} onChange={(e) => update('juicefsRsaKey', e.target.value)}
              className="input-field min-h-[60px]" placeholder="RSA encryption key for JuiceFS" required />
          </div>

          {error && (
            <div className="rounded-lg border border-red-800/50 bg-red-900/20 px-4 py-3 text-sm text-red-400">{error}</div>
          )}

          <div className="flex gap-3 justify-end pt-2">
            <button type="button" onClick={onClose} className="btn-secondary">Cancel</button>
            <button type="submit" className="btn-primary" disabled={loading}>
              {loading ? 'Creating...' : 'Create'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function Projects() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);

  const fetchProjects = useCallback(async () => {
    try {
      setLoading(true);
      const data = await api.getProjects();
      setProjects(data.projects);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load projects');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchProjects();
  }, [fetchProjects]);

  return (
    <div>
      <div className="flex items-center justify-between mb-8">
        <div>
          <h1 className="text-2xl font-bold text-gray-100">Projects</h1>
          <p className="text-gray-400 mt-1">Manage your rendering projects</p>
        </div>
        <button onClick={() => setShowCreate(true)} className="btn-primary">Create Project</button>
      </div>

      {loading && (
        <div className="text-center py-12 text-gray-400">Loading projects...</div>
      )}

      {error && (
        <div className="rounded-lg border border-red-800/50 bg-red-900/20 px-4 py-3 text-sm text-red-400 mb-6">{error}</div>
      )}

      {!loading && !error && projects.length === 0 && (
        <div className="text-center py-16">
          <p className="text-gray-400 text-lg mb-4">No projects yet</p>
          <button onClick={() => setShowCreate(true)} className="btn-primary">Create your first project</button>
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
        {projects.map((project) => (
          <Link key={project.id} to={`/projects/${project.id}`}
            className="card hover:border-gray-600 transition-colors group">
            <div className="flex items-start justify-between mb-3">
              <h3 className="text-lg font-semibold text-gray-100 group-hover:text-indigo-400 transition-colors">
                {project.name}
              </h3>
              <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-green-900/50 text-green-300">
                active
              </span>
            </div>
            <p className="text-sm text-gray-500 mb-2">{project.b2Bucket}</p>
            <div className="text-sm text-gray-400">
              Created {new Date(project.createdAt).toLocaleDateString()}
            </div>
          </Link>
        ))}
      </div>

      <CreateProjectModal isOpen={showCreate} onClose={() => setShowCreate(false)} onCreated={fetchProjects} />
    </div>
  );
}

export default Projects;
