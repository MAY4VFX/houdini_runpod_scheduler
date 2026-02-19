import { useState } from 'react';
import { Link } from 'react-router-dom';
import type { Project } from '@/lib/api';

// TODO: Replace with real API data via SWR
const placeholderProjects: Project[] = [
  {
    id: '1',
    name: 'Feature Film - The Forest',
    slug: 'the-forest',
    status: 'active',
    createdAt: '2026-01-15T10:00:00Z',
    updatedAt: '2026-02-19T08:30:00Z',
  },
  {
    id: '2',
    name: 'Commercial - Car Launch',
    slug: 'car-launch',
    status: 'active',
    createdAt: '2026-02-01T14:00:00Z',
    updatedAt: '2026-02-18T16:45:00Z',
  },
  {
    id: '3',
    name: 'TV Series - Pilot',
    slug: 'tv-pilot',
    status: 'archived',
    createdAt: '2025-11-20T09:00:00Z',
    updatedAt: '2026-01-10T12:00:00Z',
  },
];

function CreateProjectModal({
  isOpen,
  onClose,
}: {
  isOpen: boolean;
  onClose: () => void;
}) {
  const [name, setName] = useState('');
  const [slug, setSlug] = useState('');

  if (!isOpen) return null;

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    // TODO: Call api.createProject({ name, slug })
    console.log('Creating project:', { name, slug });
    onClose();
  };

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50">
      <div className="card w-full max-w-md mx-4">
        <h2 className="text-lg font-semibold text-gray-100 mb-4">
          Create Project
        </h2>
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label
              htmlFor="project-name"
              className="block text-sm font-medium text-gray-300 mb-1.5"
            >
              Project Name
            </label>
            <input
              id="project-name"
              type="text"
              value={name}
              onChange={(e) => {
                setName(e.target.value);
                setSlug(
                  e.target.value
                    .toLowerCase()
                    .replace(/[^a-z0-9]+/g, '-')
                    .replace(/^-|-$/g, ''),
                );
              }}
              className="input-field"
              placeholder="My Awesome Project"
              required
              autoFocus
            />
          </div>
          <div>
            <label
              htmlFor="project-slug"
              className="block text-sm font-medium text-gray-300 mb-1.5"
            >
              Slug
            </label>
            <input
              id="project-slug"
              type="text"
              value={slug}
              onChange={(e) => setSlug(e.target.value)}
              className="input-field"
              placeholder="my-awesome-project"
              required
            />
          </div>
          <div className="flex gap-3 justify-end pt-2">
            <button type="button" onClick={onClose} className="btn-secondary">
              Cancel
            </button>
            <button type="submit" className="btn-primary">
              Create
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function Projects() {
  const [showCreate, setShowCreate] = useState(false);

  return (
    <div>
      <div className="flex items-center justify-between mb-8">
        <div>
          <h1 className="text-2xl font-bold text-gray-100">Projects</h1>
          <p className="text-gray-400 mt-1">
            Manage your rendering projects
          </p>
        </div>
        <button onClick={() => setShowCreate(true)} className="btn-primary">
          Create Project
        </button>
      </div>

      {/* Projects Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
        {placeholderProjects.map((project) => (
          <Link
            key={project.id}
            to={`/projects/${project.id}`}
            className="card hover:border-gray-600 transition-colors group"
          >
            <div className="flex items-start justify-between mb-3">
              <h3 className="text-lg font-semibold text-gray-100 group-hover:text-indigo-400 transition-colors">
                {project.name}
              </h3>
              <span
                className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
                  project.status === 'active'
                    ? 'bg-green-900/50 text-green-300'
                    : 'bg-gray-600 text-gray-300'
                }`}
              >
                {project.status}
              </span>
            </div>
            <p className="text-sm text-gray-500 mb-4">/{project.slug}</p>
            <div className="flex items-center gap-4 text-sm text-gray-400">
              <span>
                Updated{' '}
                {new Date(project.updatedAt).toLocaleDateString()}
              </span>
            </div>
          </Link>
        ))}
      </div>

      <CreateProjectModal
        isOpen={showCreate}
        onClose={() => setShowCreate(false)}
      />
    </div>
  );
}

export default Projects;
