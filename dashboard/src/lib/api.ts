const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8787';

class ApiClient {
  private token: string | null = null;

  setToken(token: string) {
    this.token = token;
    localStorage.setItem('token', token);
  }

  getToken(): string | null {
    return this.token || localStorage.getItem('token');
  }

  clearToken() {
    this.token = null;
    localStorage.removeItem('token');
  }

  async request<T = unknown>(path: string, options?: RequestInit): Promise<T> {
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
    };
    const token = this.getToken();
    if (token) {
      headers['Authorization'] = `Bearer ${token}`;
    }

    const res = await fetch(`${API_URL}${path}`, {
      ...options,
      headers: { ...headers, ...options?.headers },
    });

    if (!res.ok) {
      const errorText = await res.text();
      throw new Error(errorText || `Request failed with status ${res.status}`);
    }

    return res.json();
  }

  // Auth
  login(email: string, password: string) {
    return this.request<{ token: string; user: { id: string; email: string } }>(
      '/auth/login',
      {
        method: 'POST',
        body: JSON.stringify({ email, password }),
      },
    );
  }

  // Projects
  getProjects() {
    return this.request<Project[]>('/projects');
  }

  getProject(id: string) {
    return this.request<Project>(`/projects/${id}`);
  }

  createProject(data: CreateProjectPayload) {
    return this.request<Project>('/projects', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  // Artists
  getArtists(projectId: string) {
    return this.request<Artist[]>(`/projects/${projectId}/artists`);
  }

  addArtist(projectId: string, data: AddArtistPayload) {
    return this.request<Artist>(`/projects/${projectId}/artists`, {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  revokeArtist(projectId: string, artistId: string) {
    return this.request(`/projects/${projectId}/artists/${artistId}`, {
      method: 'DELETE',
    });
  }
}

// Types
export interface Project {
  id: string;
  name: string;
  slug: string;
  status: 'active' | 'archived';
  createdAt: string;
  updatedAt: string;
}

export interface Artist {
  id: string;
  email: string;
  name: string;
  role: 'artist' | 'lead' | 'admin';
  addedAt: string;
}

export interface RenderJob {
  id: string;
  name: string;
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled';
  progress: number;
  podId: string | null;
  startedAt: string | null;
  completedAt: string | null;
  cost: number;
}

export interface Pod {
  id: string;
  name: string;
  gpuType: string;
  status: 'running' | 'idle' | 'starting' | 'stopping' | 'terminated';
  costPerHour: number;
  uptimeHours: number;
}

export interface CreateProjectPayload {
  name: string;
  slug: string;
}

export interface AddArtistPayload {
  email: string;
  role: 'artist' | 'lead' | 'admin';
}

export const api = new ApiClient();
