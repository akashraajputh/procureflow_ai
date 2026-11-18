import { useEffect, useState } from 'react'
import { BrowserRouter, NavLink, Route, Routes } from 'react-router-dom'
import axios from 'axios'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

const API_BASE = (import.meta.env.VITE_API_BASE_URL || '/api').replace(/\/+$/, '')
const API_CONFIGURED = import.meta.env.DEV || Boolean(import.meta.env.VITE_API_BASE_URL?.trim())

function getApiErrorMessage(error: unknown) {
  if (axios.isAxiosError(error)) {
    if (error.response?.status === 404 || typeof error.response?.data === 'string') {
      return 'The API URL is incorrect or returned a web page instead of API data. Check VITE_API_BASE_URL in Netlify and redeploy.'
    }
    return error.response?.data?.detail || 'The API is unavailable. It may be starting after an idle period; please try again shortly.'
  }
  return error instanceof Error ? error.message : 'The API could not be reached.'
}

function getTokenRole(token: string) {
  try {
    const payload = token.split('.')[1]
    return JSON.parse(atob(payload.replace(/-/g, '+').replace(/_/g, '/'))).role || 'user'
  } catch {
    return 'user'
  }
}

function DashboardPage() {
  const [summary, setSummary] = useState<any>(null)
  const [policy, setPolicy] = useState<any>(null)
  const [sampleDecisions, setSampleDecisions] = useState<any[]>([])
  const [supplierList, setSupplierList] = useState<any[]>([])
  const [dashboardError, setDashboardError] = useState('')
  const [isLoading, setIsLoading] = useState(true)

  useEffect(() => {
    if (!API_CONFIGURED) {
      setIsLoading(false)
      return
    }

    Promise.all([
      axios.get(`${API_BASE}/summary`),
      axios.get(`${API_BASE}/policy`),
      axios.get(`${API_BASE}/sample-decisions`),
      axios.get(`${API_BASE}/preferred-suppliers?item_code=ITM-1048&as_of=2026-11-01`),
    ]).then(([summaryRes, policyRes, decisionsRes, suppliersRes]) => {
      if (
        !summaryRes.data || typeof summaryRes.data !== 'object'
        || !policyRes.data || typeof policyRes.data !== 'object'
        || !Array.isArray(decisionsRes.data)
        || !Array.isArray(suppliersRes.data)
      ) {
        throw new Error('Backend returned a web page instead of the expected API response. Check VITE_API_BASE_URL in Netlify.')
      }
      setSummary(summaryRes.data)
      setPolicy(policyRes.data)
      setSampleDecisions(decisionsRes.data)
      setSupplierList(suppliersRes.data)
    }).catch((error: unknown) => setDashboardError(getApiErrorMessage(error)))
      .finally(() => setIsLoading(false))
  }, [])

  const routeChart = summary
    ? Object.entries(summary.routes || {}).map(([name, count]) => ({ name, count }))
    : []

  return (
    <div className="space-y-8">
      {dashboardError && (
        <div role="alert" className="rounded-2xl border border-amber-300 bg-amber-50 p-4 text-sm text-amber-900">
          {dashboardError}
        </div>
      )}
      {isLoading && (
        <div role="status" className="rounded-2xl border border-cyan-200 bg-cyan-50 p-4 text-sm text-cyan-900">
          Loading procurement data. The free backend may need a moment to wake up.
        </div>
      )}
      <header className="rounded-3xl bg-slate-900 p-6 text-white shadow-xl">
        <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
          <div>
            <p className="text-sm uppercase tracking-[0.2em] text-cyan-300">Corvo Engineering</p>
            <h1 className="mt-2 text-4xl font-semibold">Procurement Automation Platform</h1>
          </div>
          <div className="rounded-2xl border border-cyan-400/30 bg-slate-800 px-4 py-3 text-sm text-cyan-200">
            Policy v{policy?.version ?? '5.0'} · effective {policy?.effective_date ?? '2026-04-01'}
          </div>
        </div>
      </header>

      <section className="grid gap-4 md:grid-cols-4">
        <StatCard label="Total lines" value={summary?.total_lines ?? 0} accent="cyan" />
        <StatCard label="PO decisions" value={summary?.decisions?.PO ?? 0} accent="emerald" />
        <StatCard label="Sourcing requests" value={summary?.decisions?.SOURCING_REQUEST ?? 0} accent="amber" />
        <StatCard label="Policy document" value={policy?.document ?? 'CORVO-PROC-POL-04'} accent="violet" />
      </section>

      <section className="grid gap-6 lg:grid-cols-[1.3fr_0.7fr]">
        <div className="rounded-3xl border border-slate-200 bg-white p-6 shadow-sm">
          <h2 className="mb-4 text-xl font-semibold text-slate-800">Route distribution</h2>
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={routeChart}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis dataKey="name" />
                <YAxis allowDecimals={false} />
                <Tooltip />
                <Bar dataKey="count" radius={[8, 8, 0, 0]}>
                  {routeChart.map((entry, index) => (
                    <Cell key={`${entry.name}-${index}`} fill={['#06b6d4', '#10b981', '#f59e0b', '#8b5cf6', '#f43f5e'][index % 5]} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>

        <div className="rounded-3xl border border-slate-200 bg-white p-6 shadow-sm">
          <h2 className="mb-4 text-xl font-semibold text-slate-800">Policy controls</h2>
          <ul className="space-y-3 text-sm text-slate-600">
            <li>• Active version: {policy?.version}</li>
            <li>• Effective date: {policy?.effective_date}</li>
            <li>• Plants covered: {policy?.plants?.join(', ')}</li>
            <li>• Emergency threshold: ₹{policy?.limits?.emergency_po_threshold?.toLocaleString()}</li>
            <li>• Spot PO threshold: ₹{policy?.limits?.spot_po_threshold?.toLocaleString()}</li>
            <li>• RFQ threshold: ₹{policy?.limits?.rfq_threshold?.toLocaleString()}</li>
          </ul>
        </div>
      </section>

      <section className="grid gap-6 xl:grid-cols-2">
        <div className="rounded-3xl border border-slate-200 bg-white p-6 shadow-sm">
          <h2 className="mb-4 text-xl font-semibold text-slate-800">Sample decisions</h2>
          <div className="space-y-3">
            {sampleDecisions.slice(0, 8).map((decision) => (
              <div key={`${decision.pr_no}-${decision.item_code}`} className="flex items-center justify-between rounded-2xl bg-slate-50 p-3">
                <div>
                  <p className="font-medium text-slate-700">{decision.pr_no}</p>
                  <p className="text-xs text-slate-500">{decision.item_code || 'Unmatched'} · {decision.plant}</p>
                </div>
                <div className="text-right">
                  <span className="inline-flex rounded-full bg-emerald-100 px-2 py-1 text-xs font-medium text-emerald-700">{decision.route}</span>
                  <p className="mt-1 text-xs text-slate-500">Clause {decision.clause}</p>
                </div>
              </div>
            ))}
          </div>
        </div>

        <div className="rounded-3xl border border-slate-200 bg-white p-6 shadow-sm">
          <h2 className="mb-4 text-xl font-semibold text-slate-800">Preferred supplier list</h2>
          <div className="space-y-3">
            {supplierList.map((supplier) => (
              <div key={supplier.supplier_id} className="rounded-2xl bg-slate-50 p-3">
                <div className="flex items-center justify-between">
                  <div>
                    <p className="font-medium text-slate-700">#{supplier.rank} {supplier.supplier_name}</p>
                    <p className="text-xs text-slate-500">{supplier.supplier_id}</p>
                  </div>
                  <span className="rounded-full bg-cyan-100 px-2 py-1 text-xs font-medium text-cyan-700">Score {supplier.score.toFixed(1)}</span>
                </div>
                <div className="mt-2 flex justify-between text-xs text-slate-500">
                  <span>Reference: ₹{supplier.reference_price?.toLocaleString() ?? '—'}</span>
                  <span>On-time: {supplier.on_time_pct}%</span>
                  <span>Quality: {supplier.quality_rating}</span>
                </div>
              </div>
            ))}
          </div>
        </div>
      </section>
    </div>
  )
}

function AdminPage() {
  const [token, setToken] = useState<string>(() => localStorage.getItem('corvo-token') || '')
  const [role, setRole] = useState(() => getTokenRole(localStorage.getItem('corvo-token') || ''))
  const [loginForm, setLoginForm] = useState({ username: 'admin', password: 'admin123' })
  const [summary, setSummary] = useState<any>(null)
  const [users, setUsers] = useState<any[]>([])
  const [policies, setPolicies] = useState<any[]>([])
  const [audit, setAudit] = useState<any[]>([])
  const [approvals, setApprovals] = useState<any[]>([])
  const [error, setError] = useState('')
  const [batchFile, setBatchFile] = useState<File | null>(null)
  const [batchResult, setBatchResult] = useState<any>(null)
  const [batchError, setBatchError] = useState('')
  const [batchLoading, setBatchLoading] = useState(false)

  const loadAdminData = async (authToken: string) => {
    if (!authToken) return
    if (!API_CONFIGURED) {
      setError('Backend API is not configured. Set VITE_API_BASE_URL in Netlify to your backend URL ending in /api, then redeploy the site.')
      return
    }
    const headers = { Authorization: `Bearer ${authToken}` }
    const currentRole = getTokenRole(authToken)

    try {
      const requests = [
        axios.get(`${API_BASE}/admin/dashboard`, { headers }),
        axios.get(`${API_BASE}/admin/approvals`, { headers }),
      ]

      if (currentRole === 'admin') {
        requests.push(
          axios.get(`${API_BASE}/admin/users`, { headers }),
          axios.get(`${API_BASE}/admin/policies`, { headers }),
          axios.get(`${API_BASE}/admin/audit`, { headers }),
        )
      }

      const responses = await Promise.all(requests)
      const summaryRes = responses[0]
      const approvalsRes = responses[1]
      if (!Array.isArray(approvalsRes.data)) {
        throw new Error('Backend returned an invalid approvals response. Check the API URL configured in Netlify.')
      }
      setSummary(summaryRes.data)
      setApprovals(approvalsRes.data)

      if (currentRole === 'admin') {
        const [usersRes, policiesRes, auditRes] = responses.slice(2)
        if (!Array.isArray(usersRes.data) || !Array.isArray(policiesRes.data) || !Array.isArray(auditRes.data)) {
          throw new Error('Backend returned an invalid admin response. Check the API URL configured in Netlify.')
        }
        setUsers(usersRes.data)
        setPolicies(policiesRes.data)
        setAudit(auditRes.data)
      } else {
        setUsers([])
        setPolicies([])
        setAudit([])
      }
    } catch (err: any) {
      setError(getApiErrorMessage(err) || 'Unable to load admin data.')
      if (err.response?.status === 401) {
        setToken('')
        localStorage.removeItem('corvo-token')
      }
    }
  }

  useEffect(() => {
    if (token) {
      loadAdminData(token)
    }
  }, [token])

  async function handleLogin(event: React.FormEvent) {
    event.preventDefault()
    if (!API_CONFIGURED) {
      setError('Backend API is not configured. Set VITE_API_BASE_URL in Netlify to your backend URL ending in /api, then redeploy the site.')
      return
    }
    try {
      const response = await axios.post(`${API_BASE}/auth/login`, loginForm)
      const nextToken = response.data.token
      setToken(nextToken)
      setRole(response.data.user.role)
      localStorage.setItem('corvo-token', nextToken)
      setError('')
    } catch (err: any) {
      setError(getApiErrorMessage(err) || 'Login failed.')
    }
  }

  function handleLogout() {
    setToken('')
    setRole('user')
    localStorage.removeItem('corvo-token')
    setSummary(null)
    setUsers([])
    setPolicies([])
    setAudit([])
    setApprovals([])
    setBatchFile(null)
    setBatchResult(null)
    setBatchError('')
  }

  async function handleApprovalDecision(approvalId: number, status: 'approved' | 'rejected') {
    if (!token) return
    try {
      await axios.post(`${API_BASE}/admin/approvals/${approvalId}/decision`, { status }, { headers: { Authorization: `Bearer ${token}` } })
      setApprovals((current) => current.map((approval) => approval.id === approvalId ? { ...approval, status } : approval))
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Unable to update approval status.')
    }
  }

  async function handleBatchProcess() {
      if (!batchFile || !token) return
      const formData = new FormData()
      formData.append('file', batchFile)
      setBatchLoading(true)
      setBatchError('')
      setBatchResult(null)
      try {
        const response = await axios.post(`${API_BASE}/process-batch`, formData, {
          headers: { Authorization: `Bearer ${token}` },
        })
        setBatchResult(response.data)
      } catch (err: any) {
        setBatchError(err.response?.data?.detail || 'Batch processing failed.')
      } finally {
        setBatchLoading(false)
      }
    }

  function downloadDecisionCsv() {
      if (!batchResult?.decision_csv) return
      const blob = new Blob([batchResult.decision_csv], { type: 'text/csv;charset=utf-8' })
      const href = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = href
      anchor.download = 'pr-decision-results.csv'
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      window.setTimeout(() => URL.revokeObjectURL(href), 1000)
    }
  if (!token) {
    return (
      <div className="mx-auto max-w-xl rounded-3xl border border-slate-200 bg-white p-6 shadow-sm">
        <h2 className="mb-4 text-2xl font-semibold text-slate-800">Admin access</h2>
        <form onSubmit={handleLogin} className="space-y-4">
          <label className="block">
            <span className="mb-1 block text-xs font-medium uppercase tracking-wide text-slate-500">Username or email</span>
            <input
              value={loginForm.username}
              onChange={(event) => setLoginForm({ ...loginForm, username: event.target.value })}
              className="w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-700"
            />
          </label>
          <label className="block">
            <span className="mb-1 block text-xs font-medium uppercase tracking-wide text-slate-500">Password</span>
            <input
              type="password"
              value={loginForm.password}
              onChange={(event) => setLoginForm({ ...loginForm, password: event.target.value })}
              className="w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-700"
            />
          </label>
          {error && <p className="text-sm text-red-600">{error}</p>}
          <button type="submit" className="inline-flex rounded-xl bg-slate-900 px-4 py-2 font-medium text-white hover:bg-slate-700">
            Sign in as admin
          </button>
        </form>
        <p className="mt-4 text-xs text-slate-500">Demo roles: admin / admin123 · procurement_head / proc123 · plant_manager / plant123 · buyer / buyer123</p>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between rounded-3xl border border-slate-200 bg-white p-6 shadow-sm">
        <div>
          <h2 className="text-2xl font-semibold text-slate-800">Admin console</h2>
          <p className="text-sm text-slate-500">Signed in as {summary?.user?.username || 'admin'}</p>
        </div>
        <button onClick={handleLogout} className="rounded-xl border border-slate-200 px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-100">
          Sign out
        </button>
      </div>

      <section className="grid gap-4 md:grid-cols-3">
        <StatCard label="Users" value={summary?.totals?.users ?? 0} accent="cyan" />
        <StatCard label="Policies" value={summary?.totals?.policies ?? 0} accent="emerald" />
        <StatCard label="Audit rows" value={summary?.totals?.audit_rows ?? 0} accent="violet" />
      </section>

      {role === 'admin' && (
        <section className="rounded-3xl border border-slate-200 bg-white p-6 shadow-sm">
          <div className="flex flex-col gap-4 md:flex-row md:items-end md:justify-between">
            <div>
              <h3 className="text-xl font-semibold text-slate-800">Process PR batch</h3>
              <p className="mt-1 text-sm text-slate-500">Validate, club, evaluate and consolidate uploaded CSV lines.</p>
            </div>
            <div className="flex flex-col gap-2 sm:flex-row">
              <input
                type="file"
                accept=".csv,text/csv"
                onChange={(event) => setBatchFile(event.target.files?.[0] || null)}
                className="max-w-xs rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-700"
              />
              <button
                onClick={handleBatchProcess}
                disabled={!batchFile || batchLoading}
                className="rounded-xl bg-cyan-700 px-4 py-2 text-sm font-medium text-white hover:bg-cyan-600 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {batchLoading ? 'Processing…' : 'Process CSV'}
              </button>
            </div>
          </div>
          {batchError && <p className="mt-4 text-sm text-red-600">{batchError}</p>}
          {batchResult && (
            <div className="mt-5">
              <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
                <p className="text-sm text-slate-600">
                  {batchResult.processed_rows} processed · {batchResult.not_processed_rows} not processed · {batchResult.purchase_orders_created} POs
                </p>
                <button onClick={downloadDecisionCsv} className="rounded-xl border border-slate-200 px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-100">
                  Download decisions CSV
                </button>
              </div>
              <div className="max-h-80 overflow-auto rounded-xl border border-slate-200">
                <table className="min-w-full text-left text-xs text-slate-700">
                  <thead className="sticky top-0 bg-slate-100 text-slate-500">
                    <tr>
                      <th className="p-2">PR / line</th>
                      <th className="p-2">Item</th>
                      <th className="p-2">Route</th>
                      <th className="p-2">Decision</th>
                      <th className="p-2">Group / PO</th>
                    </tr>
                  </thead>
                  <tbody>
                    {batchResult.results.map((entry: any, index: number) => (
                      <tr key={`${entry.pr_no}-${entry.pr_line}-${index}`} className="border-t border-slate-100">
                        <td className="p-2">{entry.pr_no}/{entry.pr_line}</td>
                        <td className="p-2">{entry.resolved_item_code || '—'}</td>
                        <td className="p-2">{entry.route}</td>
                        <td className="p-2">{entry.decision}</td>
                        <td className="p-2">{entry.group_id || '—'} / {entry.po_id || '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </section>
      )}

      <section className="grid gap-6 xl:grid-cols-2">
        <div className="rounded-3xl border border-slate-200 bg-white p-6 shadow-sm">
          <h3 className="mb-4 text-xl font-semibold text-slate-800">Users</h3>
          <div className="space-y-3">
            {users.map((user) => (
              <div key={user.id} className="rounded-2xl bg-slate-50 p-3">
                <div className="flex items-center justify-between">
                  <div>
                    <p className="font-medium text-slate-700">{user.username}</p>
                    <p className="text-xs text-slate-500">{user.email}</p>
                  </div>
                  <span className="rounded-full bg-cyan-100 px-2 py-1 text-xs font-medium text-cyan-700">{user.role}</span>
                </div>
              </div>
            ))}
          </div>
        </div>

        <div className="rounded-3xl border border-slate-200 bg-white p-6 shadow-sm">
          <h3 className="mb-4 text-xl font-semibold text-slate-800">Policy history</h3>
          <div className="space-y-3">
            {policies.map((policyEntry) => (
              <div key={policyEntry.id} className="rounded-2xl bg-slate-50 p-3">
                <div className="flex items-center justify-between">
                  <div>
                    <p className="font-medium text-slate-700">{policyEntry.version}</p>
                    <p className="text-xs text-slate-500">{policyEntry.document}</p>
                  </div>
                  <span className="text-xs text-slate-500">{policyEntry.effective_date}</span>
                </div>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section className="rounded-3xl border border-slate-200 bg-white p-6 shadow-sm">
        <h3 className="mb-4 text-xl font-semibold text-slate-800">Approval queue</h3>
        <div className="space-y-3">
          {approvals.length === 0 ? (
            <p className="text-sm text-slate-500">No pending approval requests.</p>
          ) : (
            approvals.map((approval) => (
              <div key={approval.id} className="rounded-2xl border border-slate-200 bg-slate-50 p-4">
                <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
                  <div>
                    <p className="font-medium text-slate-700">{approval.pr_no || 'Local PR'} · {approval.item_code || 'Unmatched item'}</p>
                    <p className="text-xs text-slate-500">{approval.route} · {approval.decision} · {approval.clause}</p>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="rounded-full bg-violet-100 px-2 py-1 text-[10px] font-medium uppercase tracking-wide text-violet-700">{approval.required_role}</span>
                    <span className={`inline-flex rounded-full px-2 py-1 text-xs font-medium ${approval.status === 'approved' ? 'bg-emerald-100 text-emerald-700' : approval.status === 'rejected' ? 'bg-red-100 text-red-700' : 'bg-amber-100 text-amber-700'}`}>
                      {approval.status}
                    </span>
                  </div>
                </div>
                <p className="mt-2 text-sm text-slate-600">{approval.reason}</p>
                {approval.status === 'pending' && (
                  <div className="mt-3 flex gap-2">
                    <button onClick={() => handleApprovalDecision(approval.id, 'approved')} className="rounded-xl bg-emerald-600 px-3 py-2 text-xs font-medium text-white hover:bg-emerald-500">
                      Approve
                    </button>
                    <button onClick={() => handleApprovalDecision(approval.id, 'rejected')} className="rounded-xl bg-red-600 px-3 py-2 text-xs font-medium text-white hover:bg-red-500">
                      Reject
                    </button>
                  </div>
                )}
              </div>
            ))
          )}
        </div>
      </section>

      <section className="rounded-3xl border border-slate-200 bg-white p-6 shadow-sm">
        <h3 className="mb-4 text-xl font-semibold text-slate-800">Decision audit</h3>
        <div className="overflow-x-auto">
          <table className="min-w-full text-left text-sm text-slate-700">
            <thead>
              <tr className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-500">
                <th className="pb-2 pr-4">PR</th>
                <th className="pb-2 pr-4">Item</th>
                <th className="pb-2 pr-4">Route</th>
                <th className="pb-2 pr-4">Decision</th>
                <th className="pb-2 pr-4">Clause</th>
              </tr>
            </thead>
            <tbody>
              {audit.map((entry) => (
                <tr key={entry.id} className="border-b border-slate-100 last:border-b-0">
                  <td className="py-2 pr-4">{entry.pr_no || '—'}</td>
                  <td className="py-2 pr-4">{entry.item_code || '—'}</td>
                  <td className="py-2 pr-4">{entry.route}</td>
                  <td className="py-2 pr-4">{entry.decision}</td>
                  <td className="py-2 pr-4">{entry.clause}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  )
}

function ChatPage() {
  const [requestText, setRequestText] = useState('I need 200 nitrile gloves for the Chakan plant by next Friday.')
  const [intent, setIntent] = useState<any>(null)
  const [decision, setDecision] = useState<any>(null)
  const [form, setForm] = useState({
    pr_no: 'PR-26-00001',
    pr_line: 1,
    created_at: '2026-11-16 09:00',
    requester: 'S. Patil',
    department: 'Operations',
    plant: 'CHK1',
    item_code: '',
    description: 'Nitrile coated gloves',
    quantity: 200,
    uom: 'PAIR',
    estimated_unit_price: 0,
    need_by_date: '2026-11-27',
    urgency: 'Normal',
  })

  async function handleIntent() {
    const response = await axios.post(`${API_BASE}/analyze-intent`, { request_text: requestText })
    setIntent(response.data)
    setForm((current) => ({
      ...current,
      plant: response.data.plant ?? current.plant,
      description: response.data.description ?? current.description,
      quantity: response.data.quantity ?? current.quantity,
    }))
  }

  async function handleEvaluate() {
    const response = await axios.post(`${API_BASE}/evaluate-requirement`, form)
    setDecision(response.data)
  }

  return (
    <div className="grid gap-6 xl:grid-cols-[0.95fr_1.05fr]">
      <div className="rounded-3xl border border-slate-200 bg-white p-6 shadow-sm">
        <h2 className="mb-4 text-xl font-semibold text-slate-800">Conversational intake</h2>
        <textarea
          value={requestText}
          onChange={(event) => setRequestText(event.target.value)}
          className="h-32 w-full rounded-2xl border border-slate-200 bg-slate-50 p-3 text-sm text-slate-700 outline-none ring-0 focus:border-cyan-500"
        />
        <button onClick={handleIntent} className="mt-4 inline-flex rounded-xl bg-cyan-600 px-4 py-2 font-medium text-white hover:bg-cyan-500">
          Analyze request
        </button>

        {intent && (
          <div className="mt-6 rounded-2xl bg-cyan-50 p-4 text-sm text-slate-700">
            <p><strong>Plant:</strong> {intent.plant}</p>
            <p><strong>Quantity:</strong> {intent.quantity}</p>
            <p><strong>Item match:</strong> {intent.item_match ? intent.item_match.description : 'Not found in item master'}</p>
            <p><strong>Follow-up:</strong> {intent.suggested_follow_up}</p>
          </div>
        )}
      </div>

      <div className="rounded-3xl border border-slate-200 bg-white p-6 shadow-sm">
        <h2 className="mb-4 text-xl font-semibold text-slate-800">Requirement preview</h2>
        <div className="grid gap-3 md:grid-cols-2">
          <Field label="Plant" value={form.plant} onChange={(value) => setForm({ ...form, plant: value })} />
          <Field label="Item code" value={form.item_code} onChange={(value) => setForm({ ...form, item_code: value })} />
          <Field label="Description" value={form.description} onChange={(value) => setForm({ ...form, description: value })} span2 />
          <Field label="Quantity" value={String(form.quantity)} onChange={(value) => setForm({ ...form, quantity: Number(value) })} />
          <Field label="UOM" value={form.uom} onChange={(value) => setForm({ ...form, uom: value })} />
          <Field label="Need by" value={form.need_by_date} onChange={(value) => setForm({ ...form, need_by_date: value })} />
          <Field label="Urgency" value={form.urgency} onChange={(value) => setForm({ ...form, urgency: value })} />
        </div>

        <button onClick={handleEvaluate} className="mt-5 inline-flex rounded-xl bg-slate-900 px-4 py-2 font-medium text-white hover:bg-slate-700">
          Evaluate requirement
        </button>

        {decision && (
          <div className="mt-6 rounded-2xl border border-emerald-200 bg-emerald-50 p-4 text-sm text-slate-700">
            <p><strong>Decision:</strong> {decision.decision}</p>
            <p><strong>Route:</strong> {decision.route}</p>
            <p><strong>Clause:</strong> {decision.clause}</p>
            <p><strong>Requirement value:</strong> ₹{decision.requirement_value?.toLocaleString()}</p>
            <p className="mt-2"><strong>Explanation:</strong> {decision.explanation}</p>
          </div>
        )}
      </div>
    </div>
  )
}

function PolicyPage() {
  const [policy, setPolicy] = useState<any>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!API_CONFIGURED) {
      setError('Backend API is not configured. Set VITE_API_BASE_URL in Netlify to your backend URL ending in /api, then redeploy the site.')
      return
    }
    axios.get(`${API_BASE}/policy`)
      .then((res) => {
        if (!res.data || typeof res.data !== 'object' || Array.isArray(res.data)) {
          throw new Error('Backend returned an invalid policy response. Check the API URL configured in Netlify.')
        }
        setPolicy(res.data)
      })
      .catch((requestError: unknown) => setError(getApiErrorMessage(requestError)))
  }, [])

  if (error) return <div role="alert" className="rounded-2xl border border-amber-300 bg-amber-50 p-4 text-sm text-amber-900">{error}</div>
  if (!policy) return <div role="status" className="rounded-3xl bg-white p-6 text-slate-700">Loading policy…</div>

  return (
    <div className="grid gap-6 lg:grid-cols-[0.9fr_1.1fr]">
      <div className="rounded-3xl border border-slate-200 bg-white p-6 shadow-sm">
        <h2 className="mb-4 text-xl font-semibold text-slate-800">Policy metadata</h2>
        <ul className="space-y-2 text-sm text-slate-600">
          <li><strong>Document:</strong> {policy.document}</li>
          <li><strong>Version:</strong> {policy.version}</li>
          <li><strong>Effective date:</strong> {policy.effective_date}</li>
          <li><strong>Owner:</strong> {policy.owner}</li>
          <li><strong>Applied plants:</strong> {Array.isArray(policy.plants) ? policy.plants.join(', ') : 'Not specified'}</li>
        </ul>
      </div>
      <div className="rounded-3xl border border-slate-200 bg-white p-6 shadow-sm">
        <h2 className="mb-4 text-xl font-semibold text-slate-800">Versioned policy rules</h2>
        <pre className="max-h-[600px] overflow-auto rounded-2xl bg-slate-900 p-4 text-xs text-slate-200">{JSON.stringify(policy.rule_priority, null, 2)}</pre>
      </div>
    </div>
  )
}

function Field({
  label,
  value,
  onChange,
  span2 = false,
}: {
  label: string
  value: string
  onChange: (value: string) => void
  span2?: boolean
}) {
  return (
    <label className={span2 ? 'md:col-span-2' : ''}>
      <span className="mb-1 block text-xs font-medium uppercase tracking-wide text-slate-500">{label}</span>
      <input
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-700 outline-none focus:border-cyan-500"
      />
    </label>
  )
}

function StatCard({ label, value, accent }: { label: string; value: string | number; accent: 'cyan' | 'emerald' | 'amber' | 'violet' }) {
  const colors = {
    cyan: 'bg-cyan-50 text-cyan-700',
    emerald: 'bg-emerald-50 text-emerald-700',
    amber: 'bg-amber-50 text-amber-700',
    violet: 'bg-violet-50 text-violet-700',
  }

  return (
    <div className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
      <p className="text-sm text-slate-500">{label}</p>
      <div className={`mt-3 inline-flex rounded-full px-3 py-1 text-sm font-semibold ${colors[accent]}`}>
        {value}
      </div>
    </div>
  )
}

function App() {
  return (
    <BrowserRouter>
      <div className="min-h-screen bg-slate-100 text-slate-900">
        <div className="mx-auto max-w-7xl px-4 py-8">
          {!API_CONFIGURED && (
            <div role="alert" className="mb-4 rounded-2xl border border-amber-300 bg-amber-50 p-4 text-sm text-amber-900">
              Backend API URL is missing. In Netlify, set <code className="font-semibold">VITE_API_BASE_URL</code> to your backend URL ending in <code className="font-semibold">/api</code>, then redeploy.
            </div>
          )}
          <nav className="mb-8 flex flex-wrap gap-3 rounded-2xl bg-white p-3 shadow-sm ring-1 ring-slate-200">
            <NavLink to="/" className={({ isActive }) => `rounded-xl px-4 py-2 text-sm font-medium ${isActive ? 'bg-slate-900 text-white' : 'text-slate-600 hover:bg-slate-100'}`}>
              Dashboard
            </NavLink>
            <NavLink to="/chat" className={({ isActive }) => `rounded-xl px-4 py-2 text-sm font-medium ${isActive ? 'bg-slate-900 text-white' : 'text-slate-600 hover:bg-slate-100'}`}>
              PR intake
            </NavLink>
            <NavLink to="/policy" className={({ isActive }) => `rounded-xl px-4 py-2 text-sm font-medium ${isActive ? 'bg-slate-900 text-white' : 'text-slate-600 hover:bg-slate-100'}`}>
              Policy engine
            </NavLink>
            <NavLink to="/admin" className={({ isActive }) => `rounded-xl px-4 py-2 text-sm font-medium ${isActive ? 'bg-slate-900 text-white' : 'text-slate-600 hover:bg-slate-100'}`}>
              Admin
            </NavLink>
          </nav>

          <Routes>
            <Route path="/" element={<DashboardPage />} />
            <Route path="/chat" element={<ChatPage />} />
            <Route path="/policy" element={<PolicyPage />} />
            <Route path="/admin" element={<AdminPage />} />
          </Routes>
        </div>
      </div>
    </BrowserRouter>
  )
}

export default App
