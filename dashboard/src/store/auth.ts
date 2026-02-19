import { create } from 'zustand';
import { api } from '../lib/api';

interface User {
  id: string;
  email: string;
}

interface AuthState {
  isAuthenticated: boolean;
  user: User | null;
  isLoading: boolean;
  error: string | null;
  login: (email: string, password: string) => Promise<void>;
  logout: () => void;
  checkAuth: () => void;
}

export const useAuthStore = create<AuthState>((set) => ({
  isAuthenticated: false,
  user: null,
  isLoading: false,
  error: null,

  login: async (email: string, password: string) => {
    set({ isLoading: true, error: null });
    try {
      const response = await api.login(email, password);
      api.setToken(response.token);
      set({
        isAuthenticated: true,
        user: response.user,
        isLoading: false,
        error: null,
      });
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Login failed';
      set({ isLoading: false, error: message });
      throw err;
    }
  },

  logout: () => {
    api.clearToken();
    set({ isAuthenticated: false, user: null, error: null });
  },

  checkAuth: () => {
    const token = api.getToken();
    if (token) {
      // TODO: validate token with API and fetch user info
      // For now, just check if token exists
      set({ isAuthenticated: true });
    }
  },
}));
