import { Component, StrictMode, type ErrorInfo, type ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'

class AppErrorBoundary extends Component<{ children: ReactNode }, { hasError: boolean }> {
  state = { hasError: false }

  static getDerivedStateFromError() {
    return { hasError: true }
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    console.error('Application render failed:', error, errorInfo.componentStack)
  }

  render() {
    if (this.state.hasError) {
      return (
        <main className="mx-auto mt-12 max-w-2xl rounded-2xl border border-red-200 bg-white p-6 text-slate-800 shadow-sm">
          <h1 className="text-xl font-semibold">The page couldn’t be displayed</h1>
          <p className="mt-2 text-sm text-slate-600">
            Refresh the page. If this keeps happening, redeploy the latest frontend and check the Netlify deploy logs.
          </p>
        </main>
      )
    }

    return this.props.children
  }
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <AppErrorBoundary>
      <App />
    </AppErrorBoundary>
  </StrictMode>,
)
