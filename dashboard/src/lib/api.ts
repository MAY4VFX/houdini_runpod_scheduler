const API_URL = import.meta.env.VITE_API_URL || '/api';

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

    if (res.status === 401) {
      this.clearToken();
      window.location.href = '/login';
      throw new Error('Session expired');
    }

    if (!res.ok) {
      const body = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
      throw new Error(body.error || `Request failed with status ${res.status}`);
    }

    return res.json();
  }

  // Auth
  login(email: string, password: string) {
    return this.request<{ token: string; admin: { id: string; email: string } }>(
      '/auth/login',
      { method: 'POST', body: JSON.stringify({ email, password }) },
    );
  }

  // Projects
  getProjects() {
    return this.request<{ projects: Project[] }>('/projects');
  }

  createProject(data: CreateProjectPayload) {
    return this.request<{ project: Project }>('/projects', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  // Artists
  getArtists(projectId: string) {
    return this.request<{ artists: Artist[] }>(`/projects/${projectId}/artists`);
  }

  addArtist(projectId: string, data: { name: string; email: string }) {
    return this.request<{ artist: ArtistWithKey }>(`/projects/${projectId}/artists`, {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  revokeArtist(projectId: string, artistId: string) {
    return this.request<{ message: string }>(`/projects/${projectId}/artists/${artistId}`, {
      method: 'DELETE',
    });
  }

  // Monitoring
  getMonitoringJobs(projectId: string) {
    return this.request<{ jobs: MonitoringJob[]; pendingQueues: number }>(
      `/monitoring/jobs/${projectId}`,
    );
  }

  getMonitoringPods(projectId: string) {
    return this.request<{ pods: MonitoringPod[]; totalHeartbeats: number }>(
      `/monitoring/pods/${projectId}`,
    );
  }
}

// Types matching actual server responses
export interface Project {
  id: string;
  name: string;
  adminId: string;
  b2Endpoint: string;
  b2Bucket: string;
  createdAt: string;
}

export interface Artist {
  id: string;
  name: string;
  email: string;
  projectId: string;
  createdAt: string;
  revokedAt?: string;
}

export interface ArtistWithKey extends Artist {
  apiKey: string;
}

export interface MonitoringJob {
  id: string;
  status: string;
  data: Record<string, unknown>;
}

export interface MonitoringPod {
  id: string;
  alive: boolean;
  data: Record<string, unknown>;
}

export interface CreateProjectPayload {
  name: string;
  redisUrl: string;
  b2Endpoint: string;
  b2AccessKey: string;
  b2SecretKey: string;
  b2Bucket: string;
  juicefsRsaKey: string;
}

export const api = new ApiClient();
