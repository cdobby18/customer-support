import { createRoot } from "react-dom/client";
import { useEffect, useRef, useState } from "react";
import { Activity, AlertTriangle, ArrowRight, BarChart3, BookOpen, Check, CheckCircle2, CircleAlert, Clock, Download, Eye, FileText, Gauge, Gavel, Globe, Hash, Inbox, LifeBuoy, Lock, LogOut, Mail, MessageCircle, MessageSquare, Paperclip, Plus, RefreshCw, Search, Send, ShieldAlert, ShieldCheck, Sparkles, Star, Timer, Trash2, TrendingUp, UserCog, UserRound, Users, X, XCircle } from "lucide-react";
import "./styles.css";

const API_URL = import.meta.env.VITE_API_URL || "http://localhost:8000";

let onSessionExpired = null;

function setSessionExpiredHandler(handler) {
  onSessionExpired = handler;
}

async function apiRequestWithMeta(path, options = {}, token = null) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(`${API_URL}${path}`, { ...options, headers });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && token && onSessionExpired) onSessionExpired();
    const detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    throw new Error(detail || "Something went wrong");
  }
  return { body, headers: response.headers };
}

async function apiRequest(path, options = {}, token = null) {
  return (await apiRequestWithMeta(path, options, token)).body;
}

async function uploadAttachments(ticketId, files, token) {
  const form = new FormData();
  for (const file of files) form.append("files", file, file.name);
  const response = await fetch(`${API_URL}/tickets/${ticketId}/attachments`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
    body: form,
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && token && onSessionExpired) onSessionExpired();
    const detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    throw new Error(detail || "Something went wrong");
  }
  return body;
}

function slaMinutesLeft(ticket) {
  if (!ticket.sla_due_at) return Infinity;
  return (new Date(ticket.sla_due_at) - new Date()) / (1000 * 60);
}

function slaStatus(ticket) {
  if (!ticket.sla_due_at) return { label: "No SLA", class: "none" };
  const minutes = slaMinutesLeft(ticket);
  if (minutes < 0) return { label: "Overdue", class: "overdue" };
  if (minutes < 120) return { label: `${Math.round(minutes)}m left`, class: "critical" };
  if (minutes < 1440) return { label: `${Math.round(minutes / 60)}h left`, class: "warning" };
  return { label: `${Math.round(minutes / 1440)}d left`, class: "ok" };
}

function isTicketSlaOverdue(ticket) {
  return slaStatus(ticket).class === "overdue";
}

function formatDate(dateStr) {
  return new Date(dateStr).toLocaleString();
}

function priorityClass(priority) {
  const classes = { urgent: "urgent", high: "high", normal: "normal" };
  return classes[priority] || "normal";
}

const CHANNEL_META = {
  web: { label: "Web", icon: <Globe size={12} /> },
  slack: { label: "Slack", icon: <Hash size={12} /> },
  whatsapp: { label: "WhatsApp", icon: <MessageCircle size={12} /> },
  email: { label: "Email", icon: <Mail size={12} /> },
};

function channelMeta(channel) {
  return CHANNEL_META[channel] || { label: channel || "unknown", icon: <Hash size={12} /> };
}

const TOPIC_PRESETS = {
  "Billing": "I was charged for...",
  "Account access": "I can't sign in because...",
  "Refund": "I'd like a refund for...",
  "Technical issue": "I'm seeing an error when I...",
  "Other": "",
};

function AuthScreen({ onAuthenticated, banner }) {
  const [mode, setMode] = useState("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function submit(event) {
    event.preventDefault();
    setError("");
    setLoading(true);
    try {
      if (mode === "register") {
        await apiRequest("/auth/register", { method: "POST", body: JSON.stringify({ email, password }) });
      }
      const result = await apiRequest("/auth/login", { method: "POST", body: JSON.stringify({ email, password }) });
      onAuthenticated(result);
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="auth-shell">
      <section className="auth-intro">
        <div className="brand-mark"><span>R</span></div>
        <p className="eyebrow">RESOLVE / customer support</p>
        <h1>Where conversations become resolutions.</h1>
        <p className="intro-copy">Understand faster. Act smarter. Resolve better.</p>
        <div className="signal-row"><ShieldCheck size={17} /> Human oversight built into every escalation.</div>
      </section>
      <section className="auth-panel">
        <div className="auth-panel-top">
          <p className="eyebrow">Welcome back</p>
          <div className="mode-switch" role="tablist">
            <button className={mode === "login" ? "active" : ""} onClick={() => setMode("login")}>Sign in</button>
            <button className={mode === "register" ? "active" : ""} onClick={() => setMode("register")}>Create account</button>
          </div>
        </div>
        <h2>{mode === "login" ? "Enter your workspace" : "Start a support workspace"}</h2>
        <p className="muted">{mode === "login" ? "Sign in to continue to your queue." : "New accounts begin with customer access."}</p>
        <form onSubmit={submit} className="auth-form">
          <label>Email<input type="email" value={email} onChange={(event) => setEmail(event.target.value)} placeholder="user@gmail.com" required /></label>
          <label>Password<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder="At least 8 characters" minLength="8" required /></label>
          {banner && <div className="error-box" role="alert"><CircleAlert size={16} /> {banner}</div>}
          {error && <div className="error-box" role="alert"><CircleAlert size={16} /> {error}</div>}
          <button className="primary-button" disabled={loading}>{loading ? "Working..." : mode === "login" ? "Open support desk" : "Create account"}<ArrowRight size={17} /></button>
        </form>
      </section>
    </main>
  );
}

function AdminTeamPanel({ session }) {
  const [users, setUsers] = useState([]);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState("agent");
  const [notice, setNotice] = useState("");
  const [pendingDelete, setPendingDelete] = useState(null);

  async function loadUsers() {
    try { setUsers(await apiRequest("/admin/users", {}, session.access_token)); }
    catch (error) { setNotice(error.message); }
  }

  useEffect(() => { loadUsers(); }, []);

  async function createUser(event) {
    event.preventDefault();
    try {
      await apiRequest("/admin/users", { method: "POST", body: JSON.stringify({ email, password, role }) }, session.access_token);
      setEmail(""); setPassword(""); setNotice("Team member created"); await loadUsers();
    } catch (error) { setNotice(error.message); }
  }

  async function toggleUser(user) {
    try {
      await apiRequest(`/admin/users/${user.id}`, { method: "PATCH", body: JSON.stringify({ is_active: !user.is_active }) }, session.access_token);
      await loadUsers();
    } catch (error) { setNotice(error.message); }
  }

  async function deleteUser(user) {
    if (user.id === session.user.id) return;
    setPendingDelete(user);
  }

  async function confirmDeleteUser() {
    if (!pendingDelete) return;
    try {
      await apiRequest(`/admin/users/${pendingDelete.id}`, { method: "DELETE" }, session.access_token);
      setNotice("User deleted"); await loadUsers();
    } catch (error) { setNotice(error.message); }
    finally { setPendingDelete(null); }
  }

  return (
    <section className="team-panel">
      <div className="team-heading">
        <div>
          <p className="eyebrow">Administration</p>
          <h2><UserCog size={19} /> Team access</h2>
          <p className="muted">Create and manage the people who work your support queue.</p>
        </div>
        <span>{users.length} users</span>
      </div>
      {notice && <div className="team-notice" role="status">{notice}</div>}
      <form className="team-form" onSubmit={createUser}>
        <div className="form-group">
          <label>Email<input type="email" value={email} onChange={(event) => setEmail(event.target.value)} placeholder="staff@company.com" required /></label>
        </div>
        <div className="form-group">
          <label>Password<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder="Temporary password (min 8 chars)" minLength="8" required /></label>
        </div>
        <div className="form-group">
          <label>Role<select value={role} onChange={(event) => setRole(event.target.value)}><option value="agent">Agent</option><option value="admin">Admin</option></select></label>
        </div>
        <button className="secondary-button" type="submit"><Plus size={15} /> Add member</button>
      </form>
      <div className="team-list">
        <div className="team-list-header">
          <span className="col-user">User</span>
          <span className="col-role">Role</span>
          <span className="col-status">Status</span>
          <span className="col-actions">Actions</span>
        </div>
        {users.map((user) => (
          <div className="team-row" key={user.id}>
            <div className="col-user">
              <div className="avatar"><UserRound size={15} /></div>
              <div className="team-user"><strong>{user.email}</strong></div>
            </div>
            <div className="col-role"><span className={`role-badge ${user.role}`}>{user.role}</span></div>
            <div className="col-status"><span className={`account-state ${user.is_active ? "active" : "inactive"}`}>{user.is_active ? "Active" : "Inactive"}</span></div>
            <div className="col-actions">
              {user.id === session.user.id ? (
                <span className="current-badge">Current user</span>
              ) : (
                <>
                  <button className="team-toggle" onClick={() => toggleUser(user)}>{user.is_active ? "Deactivate" : "Activate"}</button>
                  <button className="team-toggle delete" onClick={() => deleteUser(user)}>Delete</button>
                </>
              )}
            </div>
          </div>
        ))}
        {users.length === 0 && <div className="empty-team">No team members yet. Add your first agent or admin above.</div>}
      </div>
      <ConfirmDialog open={!!pendingDelete} title="Delete team member" message={`Delete ${pendingDelete?.email ?? ""}? This cannot be undone.`} confirmLabel="Delete" danger onConfirm={confirmDeleteUser} onClose={() => setPendingDelete(null)} />
    </section>
  );
}

function AuditActivity({ session }) {
  const [logs, setLogs] = useState([]);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [loading, setLoading] = useState(false);
  const [purging, setPurging] = useState(false);
  const [confirmPurge, setConfirmPurge] = useState(false);
  const AUDIT_PAGE = 20;

  async function loadLogs(offset = 0) {
    setLoading(true);
    setError("");
    try {
      const result = await apiRequest(`/admin/audit-logs?limit=${AUDIT_PAGE}&offset=${offset}`, {}, session.access_token);
      setLogs((current) => (offset === 0 ? result : [...current, ...result]));
    } catch (requestError) { setError(requestError.message); }
    finally { setLoading(false); }
  }

  useEffect(() => { loadLogs(0); }, []);

  async function purgeLogs() {
    setPurging(true);
    setError("");
    setNotice("");
    try {
      const result = await apiRequest("/admin/audit-logs/purge", { method: "POST" }, session.access_token);
      setNotice(`Purged ${result.deleted} old events (retention ${result.retention_days}d)`);
      await loadLogs(0);
    } catch (requestError) { setError(requestError.message); }
    finally { setPurging(false); setConfirmPurge(false); }
  }

  return (
    <section className="audit-panel">
      <div className="team-heading"><div><p className="eyebrow">Traceability</p><h2><Activity size={19} /> Recent activity</h2><p className="muted">A record of important changes across the workspace.</p></div><div className="team-heading-actions"><button className="secondary-button" onClick={() => loadLogs(0)} disabled={loading}><RefreshCw size={14} /> Refresh</button><button className="secondary-button" onClick={() => setConfirmPurge(true)} disabled={purging}><Trash2 size={14} /> {purging ? "Purging..." : "Purge old"}</button></div></div>
      {notice && <div className="team-notice" role="status">{notice}</div>}
      {error ? <div className="team-notice error-text">{error}</div> : logs.length ? <div className="audit-list">{logs.map((log) => <article className="audit-row" key={log.id}><div className="audit-icon"><Activity size={14} /></div><div className="audit-copy"><strong>{log.action.replaceAll(".", " / ")}</strong><small>{log.entity_type} · {log.entity_id.slice(0, 8)} · {log.actor_id ? `by ${log.actor_id.slice(0, 8)}` : "system"}</small></div><time>{formatDate(log.created_at)}</time></article>)}</div> : <div className="empty-audit">No activity recorded yet.</div>}
      {logs.length > 0 && <button className="show-more" onClick={() => loadLogs(logs.length)} disabled={loading}>{loading ? "Loading..." : "Load more"}</button>}
      <ConfirmDialog open={confirmPurge} title="Purge audit history" message="Delete audit history beyond the retention window? This cannot be undone." confirmLabel="Purge" danger onConfirm={purgeLogs} onClose={() => setConfirmPurge(false)} />
    </section>
  );
}

function FeedbackDrillDown({ session }) {
  const [items, setItems] = useState(null);
  const [error, setError] = useState("");

  async function loadFeedback() {
    setError("");
    try {
      setItems(await apiRequest("/admin/analytics/feedback-details", {}, session.access_token));
    } catch (requestError) { setError(requestError.message); }
  }

  useEffect(() => { loadFeedback(); }, [session.access_token]);

  return (
    <section className="analytics-panel">
      <div className="team-heading">
        <div>
          <p className="eyebrow">Customer feedback</p>
          <h2><Star size={19} /> CSAT reports</h2>
          <p className="muted">Individual ratings and notes left on resolved conversations.</p>
        </div>
        <button className="secondary-button" onClick={loadFeedback}><RefreshCw size={14} /> {items === null ? "Loading..." : `Refresh (${items.length})`}</button>
      </div>
      {error && <div className="team-notice error-text">{error}</div>}
      {items === null ? (
        <div className="empty-state"><Star size={28} /><strong>Loading feedback...</strong></div>
      ) : items.length ? (
        <div className="audit-list">
          {items.map((entry) => (
            <article className="audit-row" key={entry.id}>
              <div className="feedback-rating" aria-label={`${entry.rating} star${entry.rating > 1 ? "s" : ""}`}>
                {[1, 2, 3, 4, 5].map((star) => <Star key={star} size={14} className={star <= entry.rating ? "filled" : ""} />)}
              </div>
              <div className="audit-copy">
                <strong>{entry.message || "Ticket removed"}</strong>
                <small>{entry.customer_id || "unknown customer"}{entry.comment ? ` · "${entry.comment}"` : ""}</small>
              </div>
              <time>{formatDate(entry.created_at)}</time>
            </article>
          ))}
        </div>
      ) : (
        <div className="empty-audit">No feedback recorded yet.</div>
      )}
    </section>
  );
}

function BreakdownBars({ items }) {
  const max = Math.max(...items.map((item) => item.count), 1);
  return (
    <div className="breakdown">
      {items.length ? items.map((item) => (
        <div className="breakdown-row" key={item.value}>
          <span className="breakdown-label" title={item.value}>{item.value || "unassigned"}</span>
          <div className="breakdown-track"><div className="breakdown-fill" style={{ width: item.count === 0 ? 0 : `${Math.max((item.count / max) * 100, 6)}%` }} /></div>
          <span className="breakdown-count">{item.count}</span>
        </div>
      )) : <div className="empty-audit">No data yet.</div>}
    </div>
  );
}

function StatCard({ icon, label, value, sub, tone }) {
  return (
    <article className={`analytics-stat ${tone || ""}`}>
      <div className="analytics-stat-icon">{icon}</div>
      <div className="analytics-stat-copy"><span>{label}</span><strong>{value}</strong>{sub && <small>{sub}</small>}</div>
    </article>
  );
}

function AnalyticsView({ session }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    apiRequest("/admin/analytics/dashboard", {}, session.access_token)
      .then(setData)
      .catch((requestError) => setError(requestError.message));
  }, [session.access_token]);

  if (error) return <section className="analytics-panel"><div className="team-notice error-text">{error}</div></section>;
  if (!data) return <section className="analytics-panel"><div className="empty-state"><BarChart3 size={28} /><strong>Loading analytics...</strong></div></section>;

  const { sla, csat, escalation, workload, channels, priorities, intents, sentiments, confidence } = data;
  const pct = (value) => (value === null || value === undefined ? "—" : `${(value * 100).toFixed(1)}%`);
  const ratingBars = [5, 4, 3, 2, 1].map((star) => ({ value: `${star} star`, count: csat.rating_distribution?.[String(star)] || 0 }));

  return (
    <section className="analytics-panel">
      <div className="team-heading">
        <div>
          <p className="eyebrow">Operations</p>
          <h2><BarChart3 size={19} /> Analytics</h2>
          <p className="muted">Service health, satisfaction and team load at a glance.</p>
        </div>
        <span>{new Date().toLocaleDateString()}</span>
      </div>

      <div className="analytics-stats">
        <StatCard icon={<Gauge size={17} />} label="Avg resolution" value={sla.average_resolution_hours === null ? "—" : `${sla.average_resolution_hours}h`} sub={`${sla.resolved_tickets} resolved of ${sla.total_tickets} total`} tone="teal" />
        <StatCard icon={<Timer size={17} />} label="Overdue SLA" value={sla.overdue_tickets} sub="open tickets past deadline" tone="danger" />
        <StatCard icon={<Star size={17} />} label="Avg rating" value={csat.average_rating === null ? "—" : `${csat.average_rating} / 5`} sub={`${csat.total_feedback} responses`} tone="amber" />
        <StatCard icon={<TrendingUp size={17} />} label="Deflection rate" value={pct(csat.deflection_rate)} sub={`response rate ${pct(csat.response_rate)}`} tone="teal" />
        <StatCard icon={<AlertTriangle size={17} />} label="Escalation rate" value={pct(escalation.escalation_rate)} sub={`${escalation.pending_review} pending review`} tone="danger" />
        <StatCard icon={<CircleAlert size={17} />} label="Needs review" value={escalation.pending_review} sub={`${escalation.approved} approved · ${escalation.rejected} rejected`} tone="amber" />
      </div>

      <div className="analytics-grid">
        <article className="analytics-card">
          <header><h4><Star size={15} /> CSAT distribution</h4></header>
          <BreakdownBars items={ratingBars} />
        </article>
        <article className="analytics-card">
          <header><h4><Activity size={15} /> Triage confidence</h4></header>
          <BreakdownBars items={[
            { value: "0.90+", count: confidence.very_high },
            { value: "0.80 - 0.89", count: confidence.high },
            { value: "0.70 - 0.79", count: confidence.medium },
            { value: "below 0.70", count: confidence.low },
          ]} />
          <p className="analytics-footnote">Average triage confidence: {confidence.average === null ? "—" : confidence.average.toFixed(2)}</p>
        </article>
        <article className="analytics-card">
          <header><h4><Users size={15} /> Team workload</h4></header>
          <BreakdownBars items={workload.map((entry) => ({ value: entry.assignee_id ? `agent ${entry.assignee_id.slice(0, 8)}` : "Unassigned", count: entry.assigned_tickets }))} />
        </article>
        <article className="analytics-card">
          <header><h4><Inbox size={15} /> Channels</h4></header>
          <BreakdownBars items={channels} />
        </article>
        <article className="analytics-card">
          <header><h4><Hash size={15} /> Priorities</h4></header>
          <BreakdownBars items={priorities} />
        </article>
        <article className="analytics-card">
          <header><h4><FileText size={15} /> Intents</h4></header>
          <BreakdownBars items={intents} />
        </article>
      </div>

      <article className="analytics-card">
        <header><h4><MessageSquare size={15} /> Customer sentiment</h4></header>
        <BreakdownBars items={sentiments} />
      </article>
    </section>
  );
}

function LlmUsageView({ session }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [resetting, setResetting] = useState(false);
  const [confirmReset, setConfirmReset] = useState(false);

  async function loadUsage() {
    try { setData(await apiRequest("/admin/llm/usage", {}, session.access_token)); }
    catch (requestError) { setError(requestError.message); }
  }

  useEffect(() => { loadUsage(); }, [session.access_token]);

  async function resetUsage() {
    setResetting(true);
    setError("");
    try { setData(await apiRequest("/admin/llm/usage/reset", { method: "POST" }, session.access_token)); }
    catch (requestError) { setError(requestError.message); }
    finally { setResetting(false); setConfirmReset(false); }
  }

  if (error) return <section className="analytics-panel"><div className="team-notice error-text">{error}</div></section>;
  if (!data) return <section className="analytics-panel"><div className="empty-state"><Gauge size={28} /><strong>Loading LLM usage...</strong></div></section>;

  return (
    <section className="analytics-panel">
      <div className="team-heading">
        <div>
          <p className="eyebrow">Model spend</p>
          <h2><Gauge size={19} /> LLM usage</h2>
          <p className="muted">Token and cost counters tracked by the LLM gateway.</p>
        </div>
        <button className="secondary-button" onClick={() => setConfirmReset(true)} disabled={resetting}>{resetting ? "Resetting..." : "Reset counters"}</button>
      </div>
      <div className="analytics-stats">
        <StatCard icon={<MessageSquare size={17} />} label="Total calls" value={data.total_calls} tone="teal" />
        <StatCard icon={<FileText size={17} />} label="Prompt tokens" value={data.total_prompt_tokens.toLocaleString()} tone="teal" />
        <StatCard icon={<Sparkles size={17} />} label="Completion tokens" value={data.total_completion_tokens.toLocaleString()} tone="teal" />
        <StatCard icon={<Gauge size={17} />} label="Est. cost" value={`$${data.total_cost_usd.toFixed(4)}`} tone="amber" />
      </div>
      {data.since && <p className="analytics-footnote">Counters collected since {data.since}</p>}
      <article className="analytics-card">
        <header><h4><Activity size={15} /> Usage by model</h4></header>
        <div className="usage-model-list">
          {data.by_model.length ? data.by_model.map((entry) => (
            <div className="usage-model-row" key={entry.model}>
              <div><strong>{entry.model || "unset"}</strong><span className="usage-model-meta">{entry.calls} calls · {entry.prompt_tokens.toLocaleString()} prompt · {entry.completion_tokens.toLocaleString()} completion</span></div>
              <span className="usage-model-cost">${entry.cost_usd.toFixed(4)}</span>
            </div>
          )) : <div className="empty-audit">No LLM calls recorded yet.</div>}
        </div>
      </article>
      <ConfirmDialog open={confirmReset} title="Reset LLM counters" message="Reset all call, token and cost counters? This cannot be undone." confirmLabel="Reset" danger onConfirm={resetUsage} onClose={() => setConfirmReset(false)} />
    </section>
  );
}

function EscalationReviewPanel({ session }) {
  const [escalations, setEscalations] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [loading, setLoading] = useState(true);
  const [notice, setNotice] = useState("");
  const [reviewReason, setReviewReason] = useState("");
  const [reviewAction, setReviewAction] = useState("");
  const [reviewLoading, setReviewLoading] = useState(false);
  const [limit, setLimit] = useState(20);
  const [total, setTotal] = useState(0);
  const [hasMore, setHasMore] = useState(false);

  const selectedEscalation = escalations.find((e) => e.id === selectedId) || null;

  async function loadEscalations() {
    setLoading(true);
    try {
      // Filtered and paginated server-side: this used to fetch every ticket in
      // the table and pick the pending ones out client-side, discarding almost
      // all of them.
      const { body, headers } = await apiRequestWithMeta(
        `/tickets?escalation_status=pending&limit=${limit}`,
        {},
        session.access_token,
      );
      setEscalations(body);
      setTotal(Number(headers.get("X-Total-Count")) || body.length);
      setHasMore(headers.get("X-Has-More") === "true");
      if (!selectedId && body.length) setSelectedId(body[0].id);
      if (selectedId && !body.some((esc) => esc.id === selectedId)) setSelectedId(body[0]?.id || null);
    } catch (error) { setNotice(error.message); }
    finally { setLoading(false); }
  }

  useEffect(() => { loadEscalations(); }, [limit]);

  async function handleReview(event) {
    event.preventDefault();
    if (!selectedEscalation || !reviewReason.trim() || !reviewAction) return;
    setReviewLoading(true);
    try {
      await apiRequest(`/tickets/${selectedEscalation.id}/escalation`, {
        method: "PATCH",
        body: JSON.stringify({ status: reviewAction, reason: reviewReason })
      }, session.access_token);
      setNotice(`Escalation ${reviewAction === "approved" ? "approved" : "rejected"}`);
      setReviewReason("");
      setReviewAction("");
      await loadEscalations();
    } catch (error) { setNotice(error.message); }
    finally { setReviewLoading(false); }
  }

  function getRiskClass(level) {
    return level || "low";
  }

  return (
    <section className="escalation-review-panel">
      <div className="team-heading">
        <div>
          <p className="eyebrow">Human Review</p>
          <h2><Gavel size={19} /> Escalation queue</h2>
          <p className="muted">Tickets requiring human approval before automated response or closure.</p>
        </div>
        <span>{total ? `${escalations.length} of ${total} pending` : `${escalations.length} pending`}</span>
      </div>
      {notice && <div className="team-notice" role="status">{notice}</div>}
      <div className="review-grid">
        <div className="review-list-column">
          {loading ? (
            <div className="empty-state">Loading escalations...</div>
          ) : escalations.length ? (
            <div className="review-list">
              {escalations.map((esc) => (
                <button
                  key={esc.id}
                  className={`review-row ${esc.id === selectedId ? "selected" : ""}`}
                  onClick={() => setSelectedId(esc.id)}
                >
                  <div className="review-row-top">
                    <span className={`status-dot ${esc.escalation_status}`} />
                    <AlertTriangle size={16} className="escalation-icon" />
                    <strong>{esc.intent.replace("_", " ")}</strong>
                    <span className={`priority ${priorityClass(esc.priority)}`}>{esc.priority}</span>
                    <span className={`risk-badge ${getRiskClass(esc.risk_level)}`}>{esc.risk_level || "low"}</span>
                  </div>
                  <p>{esc.message.slice(0, 100)}...</p>
                  <small>{esc.customer_id} · {formatDate(esc.created_at)}</small>
                </button>
              ))}
            </div>
          ) : (
            <div className="empty-state">
              <ShieldCheck size={28} />
              <strong>No pending escalations</strong>
              <span>All tickets are clear for automated handling.</span>
            </div>
          )}
          {!loading && hasMore && <button className="show-more" onClick={() => setLimit((value) => value + 20)} disabled={loading}>{loading ? "Loading..." : `Load more (${escalations.length} of ${total})`}</button>}
        </div>
        <div className="review-detail-column">
          {selectedEscalation ? (
            <>
              <div className="detail-header">
                <div>
                  <span className="detail-kicker">Escalation {selectedEscalation.id.slice(0, 8)}</span>
                  <h2>{selectedEscalation.message}</h2>
                  <p className="muted">Created {formatDate(selectedEscalation.created_at)} · {selectedEscalation.channel}</p>
                </div>
                <span className={`priority large ${priorityClass(selectedEscalation.priority)}`}>{selectedEscalation.priority}</span>
              </div>
              <div className="detail-meta">
                <div><span>Intent</span><strong>{selectedEscalation.intent.replace("_", " ")}</strong></div>
                <div><span>Customer</span><strong>{selectedEscalation.customer_id}</strong></div>
                <div><span>Sentiment</span><strong>{selectedEscalation.sentiment}</strong></div>
                <div><span>Confidence</span><strong>{(selectedEscalation.confidence * 100).toFixed(0)}%</strong></div>
                <div><span>Recommended Team</span><strong>{selectedEscalation.recommended_team || "Unassigned"}</strong></div>
                <div><span>SLA Deadline</span><strong>{selectedEscalation.sla_due_at ? formatDate(selectedEscalation.sla_due_at) : "Not set"}</strong></div>
              </div>
              <div className="sla-badge">
                <Clock size={14} />
                <span className={`sla-${slaStatus(selectedEscalation).class}`}>{slaStatus(selectedEscalation).label}</span>
              </div>
              <div className="escalation-intel">
                <div className="escalation-intel-header">
                  <h4><Activity size={16} /> Escalation Intelligence</h4>
                  <span className={`risk-badge large ${getRiskClass(selectedEscalation.risk_level)}`}>
                    {selectedEscalation.risk_level || "low"} risk
                  </span>
                </div>
                <div className="risk-meter">
                  <span
                    className={`risk-bar ${getRiskClass(selectedEscalation.risk_level)}`}
                    style={{ width: `${Math.min(selectedEscalation.risk_score || 0, 100)}%` }}
                  />
                </div>
                <div className="risk-stats">
                  <div><span>Risk Score</span><strong>{Math.round(selectedEscalation.risk_score || 0)}/100</strong></div>
                  <div><span>Route</span><strong>{selectedEscalation.escalation_route || "Unrouted"}</strong></div>
                  <div><span>Customer Tier</span><strong>{selectedEscalation.intake_metadata?.customer_context?.tier || "standard"}</strong></div>
                </div>
                {selectedEscalation.escalation_summary && (
                  <div className="escalation-summary">
                    <span>Why this escalated</span>
                    <p>{selectedEscalation.escalation_summary}</p>
                  </div>
                )}
              </div>
              <div className="triage-summary">
                <h4><FileText size={16} /> Triage Summary</h4>
                <p>{selectedEscalation.triage_summary || "No summary available"}</p>
              </div>
              <div className="review-form" onSubmit={handleReview}>
                <h4><Gavel size={16} /> Review Decision</h4>
                <div className="review-options">
                  <label className="review-option">
                    <input type="radio" name="action" value="approved" checked={reviewAction === "approved"} onChange={() => setReviewAction("approved")} />
                    <div className="option-card approved">
                      <CheckCircle2 size={20} /> Approve
                      <small>Allow automated response or close ticket</small>
                    </div>
                  </label>
                  <label className="review-option">
                    <input type="radio" name="action" value="rejected" checked={reviewAction === "rejected"} onChange={() => setReviewAction("rejected")} />
                    <div className="option-card rejected">
                      <XCircle size={20} /> Reject
                      <small>Return to queue for manual handling</small>
                    </div>
                  </label>
                </div>
                <label>
                  Reason (required)
                  <textarea value={reviewReason} onChange={(event) => setReviewReason(event.target.value)} placeholder="Explain your decision..." rows="3" required />
                </label>
                <button className="primary-button" type="submit" disabled={reviewLoading || !reviewAction || !reviewReason.trim()}>
                  {reviewLoading ? "Processing..." : `Submit ${reviewAction}`}
                  <ArrowRight size={16} />
                </button>
</div>
              <div className="intake-metadata">
                <h4><Eye size={16} /> Channel Metadata</h4>
                {selectedEscalation.intake_metadata && (
                  <pre className="metadata-display">{JSON.stringify(selectedEscalation.intake_metadata, null, 2)}</pre>
                )}
              </div>
            </>
          ) : (
            <div className="empty-detail">
              <Gavel size={28} />
              <strong>Select an escalation</strong>
              <span>Choose a ticket from the queue to review.</span>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

function AiDraftPanel({ session }) {
  const [tickets, setTickets] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [draft, setDraft] = useState(null);
  const [autoResult, setAutoResult] = useState(null);
  const [loading, setLoading] = useState(true);
  const [drafting, setDrafting] = useState(false);
  const [draftElapsed, setDraftElapsed] = useState(0);
  const [autoRunning, setAutoRunning] = useState(false);
  const [deciding, setDeciding] = useState(false);
  const [resolveOnApprove, setResolveOnApprove] = useState(true);
  const [notice, setNotice] = useState("");
  const [rejectOpen, setRejectOpen] = useState(false);
  const [rejectNote, setRejectNote] = useState("");
  const [limit, setLimit] = useState(20);
  const [total, setTotal] = useState(0);
  const [hasMore, setHasMore] = useState(false);

  const selectedTicket = tickets.find((ticket) => ticket.id === selectedId) || null;

  async function loadTickets() {
    setLoading(true);
    try {
      // Paginated server-side. This panel used to fetch the whole collection
      // and then throw away every closed ticket client-side.
      const { body, headers } = await apiRequestWithMeta(
        `/tickets?limit=${limit}`,
        {},
        session.access_token,
      );
      const draftable = body.filter((ticket) => ticket.status !== "closed");
      setTickets(draftable);
      setTotal(Number(headers.get("X-Total-Count")) || draftable.length);
      setHasMore(headers.get("X-Has-More") === "true");
      if (!selectedId && draftable.length) setSelectedId(draftable[0].id);
      if (selectedId && !draftable.some((ticket) => ticket.id === selectedId)) setSelectedId(draftable[0]?.id || null);
    } catch (error) { setNotice(error.message); }
    finally { setLoading(false); }
  }

  useEffect(() => { loadTickets(); }, [limit]);

  useEffect(() => { setDraft(null); setAutoResult(null); }, [selectedId]);

  async function generateDraft() {
    if (!selectedTicket) return;
    setDrafting(true);
    setDraftElapsed(0);
    setNotice("");
    const startedAt = Date.now();
    const ticker = setInterval(() => setDraftElapsed(Math.floor((Date.now() - startedAt) / 1000)), 1000);
    try {
      const result = await apiRequest(`/tickets/${selectedTicket.id}/response-draft`, { method: "POST" }, session.access_token);
      setDraft(result);
      if (result.reasons?.includes("no_knowledge_matches")) {
        setNotice("No knowledge base article matched closely enough, so this is a placeholder — a human should write the reply.");
      }
    } catch (error) { setNotice(error.message); }
    finally { clearInterval(ticker); setDrafting(false); }
  }

  async function approveDraft() {
    if (!selectedTicket || !draft?.draft) return;
    setDeciding(true);
    setNotice("");
    try {
      const result = await apiRequest(
        `/tickets/${selectedTicket.id}/draft-decision`,
        { method: "POST", body: JSON.stringify({ decision: "approve", body: draft.draft, resolve: resolveOnApprove }) },
        session.access_token,
      );
      setNotice(result.resolved ? "Draft approved — reply sent and ticket resolved." : "Draft approved — reply sent; ticket left open.");
      setDraft(null);
      setAutoResult(null);
      await loadTickets();
    } catch (error) { setNotice(error.message); }
    finally { setDeciding(false); }
  }

  async function rejectDraft() {
    if (!selectedTicket || !draft) return;
    setRejectNote("");
    setRejectOpen(true);
  }

  async function confirmRejectDraft() {
    if (!selectedTicket || !draft) return;
    setDeciding(true);
    setNotice("");
    try {
      await apiRequest(
        `/tickets/${selectedTicket.id}/draft-decision`,
        { method: "POST", body: JSON.stringify({ decision: "reject", note: rejectNote.trim() || null }) },
        session.access_token,
      );
      setNotice("Draft rejected — ticket stays with a human agent.");
      setDraft(null);
      setAutoResult(null);
      await loadTickets();
    } catch (error) { setNotice(error.message); }
    finally { setDeciding(false); setRejectOpen(false); }
  }

  async function runAutoRespond() {
    if (!selectedTicket) return;
    setAutoRunning(true);
    setNotice("");
    try {
      const result = await apiRequest(`/tickets/${selectedTicket.id}/auto-respond`, { method: "POST" }, session.access_token);
      setAutoResult(result);
      if (result.draft) setDraft({ ...result.draft, ticket_id: selectedTicket.id });
      await loadTickets();
    } catch (error) { setNotice(error.message); }
    finally { setAutoRunning(false); }
  }

  return (
    <section className="ai-draft-panel">
      <div className="team-heading">
        <div>
          <p className="eyebrow">Response Agent</p>
          <h2><Sparkles size={19} /> AI REPLY</h2>
          <p className="muted">Generate KB-grounded draft replies with citations, confidence scoring, and guardrail checks.</p>
        </div>
        <span>{drafting ? "generating..." : total ? `${tickets.length} of ${total} ticket${total === 1 ? "" : "s"}` : `${tickets.length} ticket${tickets.length === 1 ? "" : "s"}`}</span>
      </div>
      {notice && <div className="team-notice" role="status">{notice}</div>}
      <div className="review-grid">
        <div className="review-list-column">
          {loading ? (
            <div className="empty-state">Loading tickets...</div>
          ) : tickets.length ? (
            <div className="review-list">
              {tickets.map((ticket) => (
                <button
                  key={ticket.id}
                  className={`review-row ${ticket.id === selectedId ? "selected" : ""}`}
                  onClick={() => setSelectedId(ticket.id)}
                >
                  <div className="review-row-top">
                    <span className={`status-dot ${ticket.status}`} />
                    <strong>{ticket.intent.replace("_", " ")}</strong>
                    <span className={`priority ${ticket.priority}`}>{ticket.priority}</span>
                  </div>
                  <p>{ticket.message.slice(0, 100)}</p>
                  <small>{ticket.customer_id} · {new Date(ticket.created_at).toLocaleDateString()}</small>
                </button>
              ))}
            </div>
          ) : (
            <div className="empty-state">
              <Sparkles size={28} />
              <strong>No draftable tickets</strong>
              <span>Open or pending tickets will appear here.</span>
            </div>
          )}
          {/* Rendered outside the empty branch so a page of closed tickets does
              not strand the panel with no way to reach the next page. */}
          {!loading && hasMore && <button className="show-more" onClick={() => setLimit((value) => value + 20)} disabled={loading}>{loading ? "Loading..." : `Load more (${tickets.length} of ${total})`}</button>}
        </div>
        <div className="review-detail-column">
          {selectedTicket ? (
            <>
              <div className="detail-header">
                <div>
                  <span className="detail-kicker">Customer message</span>
                  <h2>{selectedTicket.message}</h2>
                  <p className="muted">{selectedTicket.intent.replace("_", " ")} · {selectedTicket.customer_id} · {selectedTicket.channel}</p>
                </div>
                <span className={`priority large ${selectedTicket.priority}`}>{selectedTicket.priority}</span>
              </div>
              <div className="draft-actions">
                <button className="primary-button draft-generate" onClick={generateDraft} disabled={drafting || deciding}>
                  <Sparkles size={16} /> {drafting ? `Generating draft... ${draftElapsed}s` : "Generate AI draft"}
                </button>
                <button className="secondary-button draft-auto" onClick={runAutoRespond} disabled={autoRunning || deciding}>
                  <CheckCircle2 size={16} /> {autoRunning ? "Running..." : "Auto-respond"}
                </button>
                {draft && (
                  <>
                    <button className="primary-button draft-approve" onClick={approveDraft} disabled={deciding}>
                      <CheckCircle2 size={16} /> {deciding ? "Sending..." : "Approve & send"}
                    </button>
                    <button className="secondary-button draft-reject" onClick={rejectDraft} disabled={deciding}>
                      <X size={16} /> Reject draft
                    </button>
                    <label className="draft-resolve-toggle">
                      <input type="checkbox" checked={resolveOnApprove} onChange={(event) => setResolveOnApprove(event.target.checked)} />
                      Resolve ticket on approve
                    </label>
                  </>
                )}
              </div>
              {drafting && draftElapsed >= 8 && (
                <div className="auto-result" role="status">
                  <><CircleAlert size={15} /> Still working — the embedding model is still loading (it warms in the background at startup and takes about a minute). Later drafts are near-instant.</>
                </div>
              )}
              {autoResult && (
                <div className={`auto-result ${autoResult.action}`}>
                  {autoResult.action === "auto_sent" ? (
                    <><CheckCircle2 size={15} /> AI replied to the customer — ticket resolved.</>
                  ) : autoResult.action === "needs_review" ? (
                    <><ShieldAlert size={15} /> Draft needs review — escalated to a human.</>
                  ) : (
                    <><CircleAlert size={15} /> Bot skipped · {autoResult.reason.replaceAll("_", " ")}.</>
                  )}
                </div>
              )}
              {draft && (
                <div className="draft-result">
                  <div className="draft-card">
                    <div className="draft-card-top">
                      <h4><MessageSquare size={15} /> Draft reply</h4>
                      {draft.needs_review ? (
                        <span className="draft-badge review"><ShieldAlert size={13} /> Needs review</span>
                      ) : (
                        <span className="draft-badge ready"><CheckCircle2 size={13} /> Ready to send</span>
                      )}
                    </div>
                    <p className="draft-text">{draft.draft}</p>
                    <div className="draft-confidence">
                      <span>Confidence</span>
                      <div className="confidence-track">
                        <div className={`confidence-fill ${draft.confidence >= 0.75 ? "high" : "low"}`} style={{ width: `${Math.round(draft.confidence * 100)}%` }} />
                      </div>
                      <strong>{Math.round(draft.confidence * 100)}%</strong>
                    </div>
                  </div>
                  {draft.reasons?.length > 0 && (
                    <div className="draft-reasons">
                      <h4><AlertTriangle size={15} /> Review reasons</h4>
                      <div className="reason-chips">
                        {draft.reasons.map((reason) => <span className="reason-chip" key={reason}>{reason.replaceAll("_", " ")}</span>)}
                      </div>
                    </div>
                  )}
                  {draft.citations?.length > 0 && (
                    <div className="draft-citations">
                      <h4><FileText size={15} /> Knowledge citations</h4>
                      {draft.citations.map((citation) => (
                        <div className="citation-row" key={citation.document_id}>
                          <span className="citation-title">{citation.title}</span>
                          <code>{citation.source}</code>
                          <span className="citation-score">{Math.round(citation.score * 100)}%</span>
                        </div>
                      ))}
                    </div>
                  )}
                  {draft.guardrail_violations?.length > 0 && (
                    <div className="draft-guardrails">
                      <h4><ShieldAlert size={15} /> Guardrail violations</h4>
                      {draft.guardrail_violations.map((violation, index) => (
                        <div className="guardrail-flag danger" key={index}>
                          <span className="guardrail-type">{violation.rule_type}</span>
                          <span className="guardrail-category">{violation.category}</span>
                          <code title={violation.description}>{violation.matched.slice(0, 40)}</code>
                        </div>
                      ))}
                    </div>
                  )}
                  <p className="draft-meta">Provider <strong>{draft.provider || "unset"}</strong> · Model <strong>{draft.model || "unset"}</strong> · template v{draft.template_version}{draft.ticket_id ? ` · ticket ${draft.ticket_id.slice(0, 8)}` : ""}</p>
                </div>
              )}
            </>
          ) : (
            <div className="empty-detail">
              <Sparkles size={28} />
              <strong>Select a ticket</strong>
              <span>Choose a conversation to draft an AI reply.</span>
            </div>
          )}
        </div>
      </div>
      <ConfirmDialog open={rejectOpen} title="Reject AI draft" message="Confirm you want to discard this draft. The ticket stays with a human agent." confirmLabel="Reject draft" danger onConfirm={confirmRejectDraft} onClose={() => setRejectOpen(false)}>
        <label className="modal-field">Reason (optional)<textarea rows="3" value={rejectNote} onChange={(event) => setRejectNote(event.target.value)} placeholder="Explain why this draft needs a human…" /></label>
      </ConfirmDialog>
    </section>
  );
}

function StarRating({ value, onSelect }) {
  return (
    <div className="star-rating">
      {[1, 2, 3, 4, 5].map((star) => (
        <button type="button" key={star} className={star <= value ? "filled" : ""} onClick={() => onSelect(star)} aria-label={`Rate ${star} star${star > 1 ? "s" : ""}`}><Star size={22} /></button>
      ))}
    </div>
  );
}

function ConfirmDialog({ open, title, message, confirmLabel = "Confirm", cancelLabel = "Cancel", danger = false, onConfirm, onClose, children }) {
  useEffect(() => {
    if (!open) return;
    const onKey = (event) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <div className="modal" role="dialog" aria-modal="true" aria-label={title}>
        <h3>{title}</h3>
        {message && <p>{message}</p>}
        {children}
        <div className="modal-actions">
          <button className="secondary-button" onClick={onClose} autoFocus>{cancelLabel}</button>
          <button className={`primary-button ${danger ? "danger-button" : ""}`} onClick={onConfirm}>{confirmLabel}</button>
        </div>
      </div>
    </div>
  );
}

function AgentAssistPanel({ session, ticketId }) {
  const [assist, setAssist] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function loadAssist() {
    if (!ticketId) return;
    setLoading(true);
    setError("");
    try {
      const result = await apiRequest(`/tickets/${ticketId}/agent-assist`, {}, session.access_token);
      setAssist(result);
    } catch (requestError) {
      setError(requestError.message);
      setAssist(null);
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className="ai-draft-panel">
      <div className="team-heading">
        <div>
          <p className="eyebrow">Agent Assist</p>
          <h2><LifeBuoy size={19} /> ASSIST</h2>
          <p className="muted">Summary, suggested replies, similar past cases, and relevant KB articles for the selected ticket.</p>
        </div>
        <span>{loading ? "loading..." : assist ? `v${assist.template_version}` : ""}</span>
      </div>
      {error && <div className="team-notice error-text">{error}</div>}
      <div className="draft-actions">
        <button className="primary-button draft-generate" onClick={loadAssist} disabled={loading || !ticketId}>
          <LifeBuoy size={16} /> {loading ? "Loading assist..." : "Load assist for selected ticket"}
        </button>
      </div>
      {!ticketId && <div className="empty-state"><LifeBuoy size={28} /><strong>No ticket selected</strong><span>Pick a ticket from the queue first.</span></div>}
      {assist && (
        <div className="draft-result">
          <div className="draft-card">
            <div className="draft-card-top">
              <h4><Sparkles size={15} /> Summary</h4>
              {assist.provider ? <span className="draft-badge ready">{assist.provider}{assist.model ? ` · ${assist.model}` : ""}</span> : <span className="draft-badge review">No LLM provider</span>}
            </div>
            <p className="draft-text">{assist.summary}</p>
            {assist.recommended_team && <p className="muted">Recommended team: <strong>{assist.recommended_team.replaceAll("_", " ")}</strong></p>}
          </div>

          <div className="draft-card">
            <div className="draft-card-top"><h4><MessageSquare size={15} /> Suggested replies</h4></div>
            {assist.suggested_replies.length ? assist.suggested_replies.map((reply, index) => (
              <div key={index} className="citation-row" style={{ display: "block" }}>
                <p className="draft-text" style={{ margin: 0 }}>{reply.text}</p>
                {reply.violations.length ? (
                  <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 6 }}>
                    {reply.violations.map((violation, vIndex) => (
                      <span key={vIndex} className="guardrail-badge"><ShieldAlert size={12} /> {violation.category}</span>
                    ))}
                  </div>
                ) : null}
              </div>
            )) : (
              <p className="muted">No replies suggested. Set LLM_PROVIDER to enable AI suggestions; the summary above still works without it.</p>
            )}
          </div>

          <div className="draft-card">
            <div className="draft-card-top"><h4><Activity size={15} /> Similar cases</h4></div>
            {assist.similar_cases.length ? assist.similar_cases.map((similar) => (
              <article className="help-result" key={similar.ticket_id}>
                <div className="help-result-title"><Activity size={15} /><strong>{similar.intent.replaceAll("_", " ")}</strong><span className="citation-score">{Math.round(similar.score * 100)}% match</span></div>
                <p>{similar.summary}</p>
                <small>{similar.ticket_id.slice(0, 8)}{similar.resolved_at ? ` · resolved ${new Date(similar.resolved_at).toLocaleDateString()}` : ""}</small>
              </article>
            )) : (
              <p className="muted">No similar resolved cases yet.</p>
            )}
          </div>

          <div className="draft-card">
            <div className="draft-card-top"><h4><BookOpen size={15} /> Knowledge base</h4></div>
            {assist.knowledge.length ? assist.knowledge.map((article) => (
              <div key={article.document_id} className="citation-row" style={{ display: "block" }}>
                <div className="citation-row" style={{ border: 0, background: "none", padding: 0, marginBottom: 4 }}>
                  <strong>{article.title}</strong>
                  <span className="citation-score">{Math.round(article.score * 100)}% match</span>
                </div>
                <p style={{ margin: "0 0 4px" }}>{article.excerpt}</p>
                <small>{article.source}</small>
              </div>
            )) : (
              <p className="muted">No knowledge base articles above the relevance threshold.</p>
            )}
          </div>
        </div>
      )}
    </section>
  );
}

function CustomerHelpCenter({ session, onOpenTicket }) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState([]);
  const [searched, setSearched] = useState(false);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState("");

  async function runSearch(event) {
    event.preventDefault();
    const q = query.trim();
    if (q.length < 2) return;
    setSearching(true);
    setError("");
    try {
      const matches = await apiRequest(`/knowledge/search?q=${encodeURIComponent(q)}`, {}, session.access_token);
      setResults(matches);
      setSearched(true);
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setSearching(false);
    }
  }

  return (
    <section className="help-panel">
      <div className="team-heading">
        <div>
          <p className="eyebrow">Self service</p>
          <h2><BookOpen size={19} /> Help center</h2>
          <p className="muted">Find instant answers in our knowledge base — often no ticket needed.</p>
        </div>
      </div>
      <form className="help-search" onSubmit={runSearch}>
        <Search size={17} />
        <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Try “refund policy”, “reset password”, “shipping”..." minLength="2" />
        <button className="secondary-button" disabled={searching}>{searching ? "Searching..." : "Search"}</button>
      </form>
      {error && <div className="team-notice error-text">{error}</div>}
      {searched && (results.length ? (
        <div className="help-results">
          {results.map((match) => (
            <article className="help-result" key={match.document_id}>
              <div className="help-result-title"><FileText size={15} /><strong>{match.title}</strong>{match.score != null && <span className="citation-score">{Math.round(match.score * 100)}% match</span>}</div>
              <p>{match.excerpt}</p>
              <small>{match.source}</small>
            </article>
          ))}
        </div>
      ) : (
        <div className="help-empty">
          <Search size={26} />
          <strong>No articles matched</strong>
          <span>Try different words, or open a conversation and we will take it from there.</span>
        </div>
      ))}
      <div className="help-cta">
        <LifeBuoy size={20} />
        <div><strong>Can't find what you need?</strong><small>A support specialist will pick it up quickly.</small></div>
        <button className="primary-button" onClick={onOpenTicket}>Open a conversation <ArrowRight size={15} /></button>
      </div>
    </section>
  );
}

function AppShell({ session, onLogout }) {
  const [tickets, setTickets] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [statusFilter, setStatusFilter] = useState("");
  const [query, setQuery] = useState("");
  const [slaMode, setSlaMode] = useState("");
  const [comments, setComments] = useState([]);
  const [notice, setNotice] = useState("");
  const [loading, setLoading] = useState(true);
  const [activeView, setActiveView] = useState("inbox");
  const [queueLimit, setQueueLimit] = useState(20);
  const [queueTotal, setQueueTotal] = useState(0);
  const [queueHasMore, setQueueHasMore] = useState(false);
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [topic, setTopic] = useState("");
  const [feedbackRating, setFeedbackRating] = useState(0);
  const [feedbackComment, setFeedbackComment] = useState("");
  const [feedbackSending, setFeedbackSending] = useState(false);
  const [feedbackSubmitted, setFeedbackSubmitted] = useState({});
  const [staff, setStaff] = useState([]);
  const [assigneeFilter, setAssigneeFilter] = useState("");
  const [isInternal, setIsInternal] = useState(true);
  const [health, setHealth] = useState("loading");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [unseen, setUnseen] = useState({});
  const [newReplyId, setNewReplyId] = useState(null);
  const [attachments, setAttachments] = useState([]);
  const [newFiles, setNewFiles] = useState([]);
  const [newTicketFiles, setNewTicketFiles] = useState([]);
  const [uploading, setUploading] = useState(false);
  const [composerBusy, setComposerBusy] = useState(false);
  const composerProps = { onFocus: () => setComposerBusy(true), onBlur: () => setComposerBusy(false) };
  // The reply and new-ticket drafts are uncontrolled on purpose: a controlled
  // value gets rewritten on every re-render, which silently drops whatever the
  // user has typed since the last state commit (queue polls, notices, uploads).
  const commentRef = useRef(null);
  const newMessageRef = useRef(null);

  const selectedTicket = tickets.find((ticket) => ticket.id === selectedId) || null;
  const isStaff = session.user.role !== "customer";

  async function loadTickets(silent = false) {
    if (!silent) setLoading(true);
    try {
      const params = new URLSearchParams({ limit: String(queueLimit) });
      if (statusFilter) params.set("status", statusFilter);
      if (debouncedQuery) params.set("q", debouncedQuery);
      const { body, headers } = await apiRequestWithMeta(`/tickets?${params.toString()}`, {}, session.access_token);
      setTickets(body);
      setQueueTotal(Number(headers.get("X-Total-Count")) || body.length);
      setQueueHasMore(headers.get("X-Has-More") === "true");
      if (!selectedId && body.length) setSelectedId(body[0].id);
      if (selectedId && !body.some((ticket) => ticket.id === selectedId)) setSelectedId(body[0]?.id || null);
    } catch (error) { if (!silent) setNotice(error.message); }
    finally { if (!silent) setLoading(false); }
  }

  const loadTicketsRef = useRef(loadTickets);
  loadTicketsRef.current = loadTickets;
  const composerBusyRef = useRef(composerBusy);
  composerBusyRef.current = composerBusy;

  useEffect(() => { loadTickets(); }, [statusFilter, activeView, queueLimit, debouncedQuery]);

  useEffect(() => {
    // Typing refetches the queue, so wait for a pause before hitting the API.
    const timer = setTimeout(() => setDebouncedQuery(query), 300);
    return () => clearTimeout(timer);
  }, [query]);

  useEffect(() => {
    let cancelled = false;
    const checkHealth = async () => {
      try {
        const response = await fetch(`${API_URL}/health`);
        const body = await response.json().catch(() => ({}));
        if (!cancelled) setHealth(body.status === "ok" ? "healthy" : "degraded");
      } catch {
        if (!cancelled) setHealth("down");
      }
    };
    checkHealth();
    const healthTimer = setInterval(checkHealth, 15000);
    const queueTimer = setInterval(() => {
      // Re-rendering mid-compose makes the controlled textareas drop whatever
      // the user has typed since the last state commit, so hold off until they
      // leave the field.
      if (composerBusyRef.current) return;
      if (activeView === "inbox" || activeView === "conversations") loadTicketsRef.current(true);
    }, 15000);
    return () => { cancelled = true; clearInterval(healthTimer); clearInterval(queueTimer); };
  }, [activeView]);

  useEffect(() => {
    if (!isStaff) return;
    apiRequest("/users/staff", {}, session.access_token)
      .then(setStaff)
      .catch(() => {});
  }, [session.access_token]);

  useEffect(() => {
    if (!selectedTicket) { setComments([]); setAttachments([]); return; }
    apiRequest(`/tickets/${selectedTicket.id}/comments`, {}, session.access_token).then(setComments).catch((error) => setNotice(error.message));
    apiRequest(`/tickets/${selectedTicket.id}/attachments`, {}, session.access_token).then(setAttachments).catch(() => setAttachments([]));
  }, [selectedId]);

  useEffect(() => {
    if (isStaff || !selectedTicket || !selectedTicket.last_message_at) return;
    localStorage.setItem(`resolveSeen:${selectedTicket.id}`, selectedTicket.last_message_at);
    if (unseen[selectedTicket.id]) setUnseen((prev) => ({ ...prev, [selectedTicket.id]: false }));
  }, [selectedId]);

  useEffect(() => {
    if (isStaff) return;
    for (const ticket of tickets) {
      if (!ticket.last_message_external || !ticket.last_message_at) continue;
      const seenAt = Date.parse(localStorage.getItem(`resolveSeen:${ticket.id}`) || "") || 0;
      if (Date.parse(ticket.last_message_at) > seenAt) {
        setUnseen((prev) => (prev[ticket.id] ? prev : { ...prev, [ticket.id]: true }));
        if (selectedId !== ticket.id) {
          setNewReplyId((prev) => prev || ticket.id);
        }
      }
    }
  }, [tickets]);

  function openNewReply() {
    if (!newReplyId) return;
    setSelectedId(newReplyId);
    const replied = tickets.find((ticket) => ticket.id === newReplyId);
    if (replied?.last_message_at) localStorage.setItem(`resolveSeen:${newReplyId}`, replied.last_message_at);
    setUnseen((prev) => ({ ...prev, [newReplyId]: false }));
    setNewReplyId(null);
  }

  function selectTicket(ticket) {
    setSelectedId(ticket.id);
    if (newReplyId === ticket.id) setNewReplyId(null);
  }

  useEffect(() => {
    if (activeView === "escalations" || activeView === "help" || !tickets.length) return;
    const matching = tickets.filter((ticket) =>
      activeView === "inbox" ? ticket.status !== "closed" :
      activeView === "conversations" ? ["resolved", "closed"].includes(ticket.status) :
      true
    );
    if (!matching.some((ticket) => ticket.id === selectedId)) setSelectedId(matching[0]?.id || null);
  }, [activeView]);

  async function createTicket(event) {
    event.preventDefault();
    const body = (newMessageRef.current?.value ?? "").trim();
    if (!body) return;
    try {
      const message = topic ? `[${topic}] ${body}` : body;
      const ticket = await apiRequest("/tickets", { method: "POST", body: JSON.stringify({ customer_id: session.user.id, message, channel: "web" }) }, session.access_token);
      if (newMessageRef.current) newMessageRef.current.value = "";
      setTopic("");
      await loadTickets(); setSelectedId(ticket.id);
      if (newTicketFiles.length) {
        setUploading(true);
        try {
          const uploaded = await uploadAttachments(ticket.id, newTicketFiles, session.access_token);
          setNewTicketFiles([]);
          setAttachments(uploaded);
          setNotice(uploaded.length ? `Ticket created with ${uploaded.length} attachment${uploaded.length > 1 ? "s" : ""}` : "Ticket created");
        } catch (error) {
          setNotice(error.message);
        } finally {
          setUploading(false);
        }
      } else {
        setNotice("Ticket created");
      }
    } catch (error) { setNotice(error.message); }
  }

  async function reloadComments() {
    if (!selectedTicket) return;
    try {
      const latest = await apiRequest(`/tickets/${selectedTicket.id}/comments`, {}, session.access_token);
      setComments(latest);
    } catch (error) { setNotice(error.message); }
  }

  async function updateTicket(nextStatus) {
    try {
      await apiRequest(`/tickets/${selectedTicket.id}`, { method: "PATCH", body: JSON.stringify({ status: nextStatus }) }, session.access_token);
      setNotice("Ticket updated"); await loadTickets(); await reloadComments();
    } catch (error) { setNotice(error.message); }
  }

  async function assignTicket(assigneeId) {
    try {
      await apiRequest(`/tickets/${selectedTicket.id}`, { method: "PATCH", body: JSON.stringify({ assignee_id: assigneeId }) }, session.access_token);
      setNotice(assigneeId ? "Ticket assigned" : "Ticket unassigned"); await loadTickets();
    } catch (error) { setNotice(error.message); }
  }

  async function addComment(event) {
    event.preventDefault();
    const body = (commentRef.current?.value ?? "").trim();
    if (!body) return;
    try {
      const created = await apiRequest(`/tickets/${selectedTicket.id}/comments`, { method: "POST", body: JSON.stringify({ author_id: session.user.id, body, is_internal: isStaff && isInternal }) }, session.access_token);
      setComments((current) => [...current, created]);
      if (commentRef.current) commentRef.current.value = "";
      if (newFiles.length) {
        setUploading(true);
        try {
          const uploaded = await uploadAttachments(selectedTicket.id, newFiles, session.access_token);
          setNewFiles([]);
          setAttachments((current) => [...current, ...uploaded]);
        } catch (error) { setNotice(error.message); }
        finally { setUploading(false); }
      }
    } catch (error) { setNotice(error.message); }
  }

  async function downloadAttachment(attachment) {
    try {
      const response = await fetch(`${API_URL}/tickets/${selectedTicket.id}/attachments/${attachment.id}/content`, {
        headers: { Authorization: `Bearer ${session.access_token}` },
      });
      if (!response.ok) throw new Error("Download failed");
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = attachment.filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    } catch (error) { setNotice(error.message); }
  }

  async function submitFeedback(ticket) {
    if (!feedbackRating) return;
    setFeedbackSending(true);
    try {
      await apiRequest(`/tickets/${ticket.id}/feedback`, { method: "POST", body: JSON.stringify({ rating: feedbackRating, comment: feedbackComment.trim() || null }) }, session.access_token);
      setFeedbackSubmitted((current) => ({ ...current, [ticket.id]: true }));
      setFeedbackRating(0); setFeedbackComment(""); setNotice("Thanks for your feedback");
    } catch (error) {
      if (error.message.includes("already exists")) {
        setFeedbackSubmitted((current) => ({ ...current, [ticket.id]: true }));
        setNotice("You've already rated this conversation");
      } else {
        setNotice(error.message);
      }
    } finally {
      setFeedbackSending(false);
    }
  }

  async function deleteTicket() {
    if (!selectedTicket) return;
    setConfirmDelete(true);
  }

  async function confirmDeleteTicket() {
    if (!selectedTicket) return;
    try {
      await apiRequest(`/tickets/${selectedTicket.id}`, { method: "DELETE" }, session.access_token);
      setNotice("Ticket deleted"); setSelectedId(null); setConfirmDelete(false); await loadTickets();
    } catch (error) { setNotice(error.message); setConfirmDelete(false); }
  }

  const viewTickets = tickets.filter((ticket) =>
    activeView === "inbox" ? ticket.status !== "closed" :
    activeView === "conversations" ? ["resolved", "closed"].includes(ticket.status) :
    true
  );
  const assigneeViewTickets = viewTickets.filter((ticket) => {
    if (assigneeFilter === "mine") return ticket.assignee_id === session.user.id;
    if (assigneeFilter === "unassigned") return !ticket.assignee_id;
    return true;
  });
  const slaViewTickets = assigneeViewTickets.filter((ticket) => {
    if (ticket.status === "resolved" || ticket.status === "closed") return true;
    const minutes = slaMinutesLeft(ticket);
    if (slaMode === "overdue") return minutes < 0;
    if (slaMode === "soon") return minutes >= 0 && minutes <= 1440;
    return true;
  });
  const sortedViewTickets =
    slaMode === "urgency"
      ? [...slaViewTickets].sort((a, b) => {
          const aResolved = a.status === "resolved" || a.status === "closed";
          const bResolved = b.status === "resolved" || b.status === "closed";
          if (aResolved || bResolved) return aResolved === bResolved ? 0 : aResolved ? 1 : -1;
          if (slaMinutesLeft(a) === slaMinutesLeft(b)) return 0;
          return slaMinutesLeft(a) - slaMinutesLeft(b);
        })
      : slaViewTickets;
  const visibleTickets = sortedViewTickets.filter((ticket) => `${ticket.message} ${ticket.intent} ${ticket.customer_id}`.toLowerCase().includes(query.toLowerCase()));
  const staffName = (userId) => staff.find((user) => user.id === userId)?.email?.split("@")[0] || null;
  const shownTickets = visibleTickets;

return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="sidebar-brand"><div className="brand-mark small"><span>R</span></div><strong>RESOLVE</strong></div>
        <div className="workspace-label">Workspace</div>
        <nav>
          <button className={`nav-item ${activeView === "inbox" ? "active" : ""}`} onClick={() => setActiveView("inbox")}><Inbox size={17} /> Inbox <span>{tickets.filter(t => t.status !== "closed").length}</span></button>
          {isStaff && <button className={`nav-item ${activeView === "escalations" ? "active" : ""}`} onClick={() => setActiveView("escalations")}><AlertTriangle size={17} /> Escalations <span>{tickets.filter(t => t.escalation_status === "pending").length}</span></button>}
          {isStaff && <button className={`nav-item ${activeView === "ai-draft" ? "active" : ""}`} onClick={() => setActiveView("ai-draft")}><Sparkles size={17} /> RelayAI</button>}
          {isStaff && <button className={`nav-item ${activeView === "agent-assist" ? "active" : ""}`} onClick={() => setActiveView("agent-assist")}><LifeBuoy size={17} /> Assist</button>}
          <button className={`nav-item ${activeView === "conversations" ? "active" : ""}`} onClick={() => setActiveView("conversations")}><MessageSquare size={17} /> Conversations <span>{tickets.filter(t => ["resolved", "closed"].includes(t.status)).length}</span></button>
          <button className={`nav-item ${activeView === "help" ? "active" : ""}`} onClick={() => setActiveView("help")}><BookOpen size={17} /> Help center</button>
          {session.user.role === "admin" && <button className={`nav-item ${activeView === "analytics" ? "active" : ""}`} onClick={() => setActiveView("analytics")}><BarChart3 size={17} /> Analytics</button>}
          {session.user.role === "admin" && <button className={`nav-item ${activeView === "feedback" ? "active" : ""}`} onClick={() => setActiveView("feedback")}><Star size={17} /> Feedback</button>}
          {session.user.role === "admin" && <button className={`nav-item ${activeView === "llm" ? "active" : ""}`} onClick={() => setActiveView("llm")}><Gauge size={17} /> LLM usage</button>}
          {session.user.role === "admin" && <button className={`nav-item ${activeView === "team" ? "active" : ""}`} onClick={() => setActiveView("team")}><UserCog size={17} /> Team</button>}
          {session.user.role === "admin" && <button className={`nav-item ${activeView === "audit" ? "active" : ""}`} onClick={() => setActiveView("audit")}><Activity size={17} /> Audit</button>}
        </nav>
        <div className="sidebar-bottom"><div className="user-chip"><div className="avatar"><UserRound size={16} /></div><div><strong>{session.user.email.split("@")[0]}</strong><small>{session.user.role}</small></div></div><button className="logout-button" onClick={onLogout} aria-label="Sign out" title="Sign out"><LogOut size={17} /></button></div>
      </aside>
      <main className="workspace">
        <header className="topbar"><div><p className="eyebrow">{isStaff ? "Agent workspace" : "Customer portal"}</p><h1>{activeView === "escalations" ? "ESCALATION QUEUE" : activeView === "conversations" ? "CONVERSATION HISTORY" : activeView === "analytics" ? "ANALYTICS" : activeView === "feedback" ? "CSAT FEEDBACK" : activeView === "llm" ? "LLM USAGE" : activeView === "ai-draft" ? "AI RESPONSE" : activeView === "agent-assist" ? "AGENT ASSIST" : activeView === "help" ? "HELP CENTER" : activeView === "team" ? "TEAM ACCESS" : activeView === "audit" ? "AUDIT LOG" : isStaff ? "SUPPORT QUEUE" : "Your conversations"}</h1></div><div className="topbar-actions"><span className={`live-status ${health}`}><span /> {health === "healthy" ? "System healthy" : health === "down" ? "API unreachable" : health === "degraded" ? "Degraded" : "Checking..."}</span><button className="icon-button" onClick={loadTickets} aria-label="Refresh tickets" title="Refresh tickets"><RefreshCw size={17} /></button></div></header>
        <section className="stats-row"><div><span>{isStaff ? "Open tickets" : "Open"}</span><strong>{tickets.filter((ticket) => ticket.status === "open").length}</strong></div><div><span>{isStaff ? "Needs attention" : "In review"}</span><strong>{tickets.filter((ticket) => ticket.requires_human_review).length}</strong></div><div><span>In progress</span><strong>{tickets.filter((ticket) => ticket.status === "in_progress").length}</strong></div></section>
        {notice && <div className="notice" role="status"><Check size={15} /> {notice}<button onClick={() => setNotice("")} aria-label="Dismiss">Dismiss</button></div>}
        {newReplyId && (
          <div className="notice reply-notice" role="status"><MessageSquare size={15} /> New reply on thread #{newReplyId.slice(0, 8)}<button onClick={openNewReply}>View</button><button onClick={() => setNewReplyId(null)} aria-label="Dismiss">Dismiss</button></div>
        )}
        {activeView === "analytics" && session.user.role === "admin" ? (
          <AnalyticsView session={session} />
        ) : activeView === "feedback" && session.user.role === "admin" ? (
          <FeedbackDrillDown session={session} />
        ) : activeView === "llm" && session.user.role === "admin" ? (
          <LlmUsageView session={session} />
        ) : activeView === "team" && session.user.role === "admin" ? (
          <AdminTeamPanel session={session} />
        ) : activeView === "audit" && session.user.role === "admin" ? (
          <AuditActivity session={session} />
        ) : activeView === "escalations" && isStaff ? (
          <EscalationReviewPanel session={session} />
        ) : activeView === "ai-draft" && isStaff ? (
          <AiDraftPanel session={session} />
        ) : activeView === "agent-assist" && isStaff ? (
          <AgentAssistPanel session={session} ticketId={selectedId} />
        ) : activeView === "help" ? (
          <CustomerHelpCenter session={session} onOpenTicket={() => setActiveView("inbox")} />
        ) : (
          <section className="desk-grid">
            <div className="ticket-column">
              <div className="toolbar"><div className="search-box"><Search size={16} /><input placeholder="Search tickets" value={query} onChange={(event) => setQuery(event.target.value)} /></div><select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}><option value="">All statuses</option><option value="open">Open</option><option value="in_progress">In progress</option><option value="pending">Pending</option><option value="resolved">Resolved</option></select><select value={slaMode} onChange={(event) => setSlaMode(event.target.value)}><option value="">All deadlines</option><option value="overdue">Overdue only</option><option value="soon">Due within 24h</option><option value="urgency">SLA urgency</option></select>{isStaff && <select value={assigneeFilter} onChange={(event) => setAssigneeFilter(event.target.value)}><option value="">All assignees</option><option value="mine">Assigned to me</option><option value="unassigned">Unassigned</option></select>}</div>
              <div className="ticket-list">{loading ? <div className="empty-state">Loading queue...</div> : visibleTickets.length ? shownTickets.map((ticket) => { const sla = slaStatus(ticket); return <button className={`ticket-row ${ticket.id === selectedId ? "selected" : ""} ${isTicketSlaOverdue(ticket) ? "overdue" : ""}`} key={ticket.id} onClick={() => selectTicket(ticket)}><div className="ticket-row-top"><span className={`status-dot ${ticket.status}`} /> <strong>{ticket.intent.replace("_", " ")}</strong>{isStaff && ticket.guardrail_status === "flagged" && <span className="guardrail-badge"><ShieldAlert size={12} /> flagged</span>}<span className={`priority ${ticket.priority}`}>{ticket.priority}</span></div><p>{ticket.message}</p><div className="ticket-row-meta"><span className={`channel-badge ${ticket.channel || "other"}`}>{channelMeta(ticket.channel).icon} {channelMeta(ticket.channel).label}</span>{!isStaff && unseen[ticket.id] && <span className="reply-badge"><MessageSquare size={11} /> New reply</span>}<span className="thread-id">#{ticket.id.slice(0, 8)}</span>{(isStaff ? <span className={`sla-chip ${sla.class}`} title={ticket.sla_due_at ? `SLA due ${formatDate(ticket.sla_due_at)}` : "No SLA deadline"}><Clock size={12} /> {sla.label}</span> : ticket.sla_due_at ? <span className={`sla-chip ${sla.class}`} title={`We aim to reply by ${formatDate(ticket.sla_due_at)}`}><Clock size={12} /> Reply by {formatDate(ticket.sla_due_at)}</span> : null)}{isStaff && ticket.assignee_id && <span className="assignee-chip" title="Assigned agent">@ {staffName(ticket.assignee_id) || ticket.assignee_id.slice(0, 6)}</span>}<small>{isStaff ? `${ticket.customer_id}` : ""}<span className="meta-sep">·</span>{new Date(ticket.created_at).toLocaleDateString()}</small></div></button>; }) : <div className="empty-state"><Inbox size={28} /><strong>{activeView === "conversations" ? "No past conversations" : "No tickets here"}</strong><span>{activeView === "conversations" ? "Resolved and closed tickets will appear here." : "New conversations will appear in this queue."}</span></div>}{queueHasMore && <button className="show-more" onClick={() => setQueueLimit((value) => value + 20)} disabled={loading}>{loading ? "Loading..." : `Load more (${shownTickets.length} of ${queueTotal})`}</button>}</div>
              {!isStaff && <form className="new-ticket" onSubmit={createTicket}><label>How can we help?<textarea {...composerProps} ref={newMessageRef} defaultValue="" placeholder="Tell us what happened..." rows="3" required /></label><label className="file-field"><Paperclip size={15} /><span>Attach files{newTicketFiles.length ? ` (${newTicketFiles.length})` : ""}</span><input type="file" multiple onChange={(event) => setNewTicketFiles([...event.target.files])} /></label><div className="topic-chips"><span>Quick topics</span>{Object.entries(TOPIC_PRESETS).map(([key, example]) => <button type="button" key={key} className={topic === key ? "active" : ""} onClick={() => { setTopic(key); if (newMessageRef.current) newMessageRef.current.value = example; }}>{key}</button>)}{topic && <button type="button" className="clear-topic" onClick={() => { setTopic(""); }}>Clear topic</button>}</div><div className="new-ticket-actions"><button className="secondary-button" type="submit"><Plus size={16} /> Submit ticket</button><button type="button" className="help-link" onClick={() => setActiveView("help")}><BookOpen size={15} /> Search help center first</button></div></form>}
            </div>
            <div className="detail-column">{selectedTicket ? (
          <>
            <div className="detail-header"><div><span className="detail-kicker">Thread #{selectedTicket.id.slice(0, 8)}</span><h2>{selectedTicket.message}</h2><p className="muted">Created {new Date(selectedTicket.created_at).toLocaleString()} · <span className={`channel-badge ${selectedTicket.channel || "other"}`}>{channelMeta(selectedTicket.channel).icon} {channelMeta(selectedTicket.channel).label}</span></p></div><div className="detail-header-actions"><span className={`priority large ${selectedTicket.priority}`}>{selectedTicket.priority}</span>{isStaff && <button className="trash-button" onClick={deleteTicket} aria-label="Delete ticket" title="Delete ticket"><Trash2 size={16} /></button>}</div></div>
            {isStaff ? (
              <div className="detail-meta"><div><span>Intent</span><strong>{selectedTicket.intent.replace("_", " ")}</strong></div><div><span>Customer</span><strong>{selectedTicket.customer_id}</strong></div><div><span>Assignee</span><strong>{staff.length ? <select className="assignee-select" value={selectedTicket.assignee_id || ""} onChange={(event) => assignTicket(event.target.value || null)} aria-label="Assign ticket"><option value="">Unassigned</option>{staff.map((user) => <option key={user.id} value={user.id}>{user.email.split("@")[0]}</option>)}</select> : (staffName(selectedTicket.assignee_id) || selectedTicket.assignee_id || "Unassigned")}</strong></div><div><span>Review</span><strong>{selectedTicket.requires_human_review ? "Human review" : "Automated"}</strong></div><div><span>Guardrail</span><strong>{selectedTicket.guardrail_status === "flagged" ? `Flagged (${selectedTicket.guardrail_hits?.length || 0})` : "Clean"}</strong></div></div>
            ) : (
              <>
                <div className="detail-meta customer-meta">
                  <div><span>Status</span><strong>{selectedTicket.status.replaceAll("_", " ")}</strong></div>
                  <div><span>Priority</span><strong>{selectedTicket.priority}</strong></div>
                  <div><span>Channel</span><strong>{selectedTicket.channel}</strong></div>
                </div>
                <div className="customer-progress">
                  <div className="progress-item done"><CheckCircle2 size={14} /><span>Received</span><strong>{new Date(selectedTicket.created_at).toLocaleString()}</strong></div>
                  <div className="progress-item"><Clock size={14} /><span>First response</span><strong>{selectedTicket.first_response_at ? new Date(selectedTicket.first_response_at).toLocaleString() : "Waiting for first reply"}</strong></div>
                  <div className="progress-item"><Timer size={14} /><span>We aim to reply by</span><strong>{selectedTicket.sla_due_at ? new Date(selectedTicket.sla_due_at).toLocaleString() : "Not set"}</strong></div>
                  {selectedTicket.requires_human_review && <div className="progress-item human"><ShieldAlert size={14} /><span>Review</span><strong>A support specialist is reviewing your issue</strong></div>}
                  {selectedTicket.resolved_at && <div className="progress-item done"><CheckCircle2 size={14} /><span>Resolved</span><strong>{new Date(selectedTicket.resolved_at).toLocaleString()}</strong></div>}
                </div>
              </>
            )}
            {isStaff && selectedTicket.guardrail_hits?.length > 0 && (
              <div className="guardrail-flags">
                <h4><ShieldAlert size={15} /> Guardrail warnings</h4>
                {selectedTicket.guardrail_hits.map((hit, index) => (
                  <div className={`guardrail-flag ${hit.severity}`} key={index}>
                    <span className="guardrail-type">{hit.rule_type}</span>
                    <span className="guardrail-category">{hit.category}</span>
                    <code title={hit.description}>{hit.matched.slice(0, 40)}</code>
                  </div>
                ))}
              </div>
            )}
            {isStaff && <div className="status-actions"><span>Move ticket</span>{["open", "in_progress", "pending", "resolved", "closed"].map((status) => <button className={selectedTicket.status === status ? "active" : ""} key={status} onClick={() => updateTicket(status)}>{status.replace("_", " ")}</button>)}</div>}
            {attachments.length ? <div className="conversation attach-list"><div className="conversation-heading"><h3>Attachments</h3><span>{attachments.length} file{attachments.length > 1 ? "s" : ""}</span></div>{attachments.map((attachment) => <div className="attachment-row" key={attachment.id}><Paperclip size={14} /><span className="attachment-name" title={attachment.filename}>{attachment.filename}</span><small>{attachment.size > 1048576 ? `${(attachment.size / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(attachment.size / 1024))} KB`}</small><button type="button" className="attachment-download" onClick={() => downloadAttachment(attachment)} aria-label={`Download ${attachment.filename}`}><Download size={14} /></button></div>)}</div> : null}
            <div className="conversation"><div className="conversation-heading"><h3>Conversation</h3><span>{comments.length} messages</span></div>{comments.length ? comments.map((item) => <article className={`message ${item.is_internal ? "internal" : ""} ${item.author_id === "ai-assistant" ? "ai" : ""}`} key={item.id}><div className="message-avatar"><Sparkles size={15} /></div><div><div className="message-meta"><strong>{item.author_id === session.user.id ? "You" : item.author_id === "ai-assistant" ? "Relay AI" : item.author_id}</strong>{item.author_id === "ai-assistant" && <span className="ai-tag">AI</span>}{item.is_internal && <span>Internal note</span>}<time>{new Date(item.created_at).toLocaleString()}</time></div><p>{item.body}</p></div></article>) : <div className="empty-conversation">No messages yet.</div>}{isStaff || !["resolved", "closed"].includes(selectedTicket.status) ? <form className="comment-form" onSubmit={addComment}>{isStaff && <div className="comment-visibility" role="group" aria-label="Comment visibility"><button type="button" className={!isInternal ? "active" : ""} onClick={() => setIsInternal(false)}><Send size={13} /> Reply to customer</button><button type="button" className={isInternal ? "active" : ""} onClick={() => setIsInternal(true)}><Lock size={13} /> Internal note</button></div>}<textarea {...composerProps} ref={commentRef} defaultValue="" placeholder={isStaff ? (isInternal ? "Write an internal note..." : "Write a customer-facing reply...") : "Write a reply..."} rows="3" /><label className="file-field"><Paperclip size={15} /><span>Attach{newFiles.length ? ` (${newFiles.length})` : " files"}</span><input type="file" multiple onChange={(event) => setNewFiles([...event.target.files])} disabled={uploading} /></label><button className="primary-button">{uploading ? "Uploading..." : !isStaff || isInternal ? "Send" : "Send reply"} <ArrowRight size={16} /></button></form> : <div className="resolved-note"><CheckCircle2 size={14} /> This conversation is resolved. Open a new one from the left for anything else.</div>}</div>
            {!isStaff && (selectedTicket.status === "resolved" || selectedTicket.status === "closed") && (
              <div className="feedback-panel">
                <h4><Star size={15} /> How did we do?</h4>
                {feedbackSubmitted[selectedTicket.id] ? <p className="feedback-thanks"><CheckCircle2 size={15} /> Thanks for your feedback!</p> : (<>
                  <StarRating value={feedbackRating} onSelect={setFeedbackRating} />
                  <textarea value={feedbackComment} onChange={(event) => setFeedbackComment(event.target.value)} placeholder="Anything we could improve? (optional)" rows="2" maxLength="1000" />
                  <button className="primary-button" onClick={() => submitFeedback(selectedTicket)} disabled={feedbackSending || !feedbackRating}>{feedbackSending ? "Submitting..." : "Submit rating"} <Send size={15} /></button>
                </>)}
              </div>
            )}
          </>
        ) : (
          <div className="empty-detail"><Inbox size={28} /><strong>Select a ticket</strong><span>Choose a conversation from the queue to view details.</span></div>
        )}</div>
          </section>
        )}
        <ConfirmDialog open={confirmDelete} title="Delete ticket" message={`Delete ${selectedTicket ? `ticket ${selectedTicket.id.slice(0, 8)}` : "this ticket"} and all its messages? This cannot be undone.`} confirmLabel="Delete" danger onConfirm={confirmDeleteTicket} onClose={() => setConfirmDelete(false)} />
      </main>
    </div>
  );
}

const SESSION_KEY = "relay-session";

function readStoredSession() {
  const raw = localStorage.getItem(SESSION_KEY);
  if (!raw) return null;
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    localStorage.removeItem(SESSION_KEY);
    return null;
  }
  const user = parsed && typeof parsed === "object" ? parsed.user : null;
  const valid =
    typeof parsed?.access_token === "string" &&
    parsed.access_token.length > 0 &&
    user !== null &&
    typeof user === "object" &&
    typeof user.id === "string" &&
    typeof user.email === "string" &&
    typeof user.role === "string";
  if (!valid) {
    localStorage.removeItem(SESSION_KEY);
    return null;
  }
  return parsed;
}

export default function App() {
  const [session, setSession] = useState(readStoredSession);
  const [banner, setBanner] = useState("");
  const handleAuthenticated = (nextSession) => { localStorage.setItem(SESSION_KEY, JSON.stringify(nextSession)); setBanner(""); setSession(nextSession); };
  const logout = () => { localStorage.removeItem(SESSION_KEY); setSession(null); };
  const logoutAndRevoke = async () => {
    const current = readStoredSession();
    try {
      // Revoke before the UI reports "signed out". Clearing local state first
      // leaves a window where the screen says the session is gone while the
      // token is still live server-side.
      if (current) await apiRequest("/auth/logout", { method: "POST" }, current.access_token);
    } catch {
      // An unreachable API must not strand the user on a screen they cannot leave.
    } finally {
      logout();
    }
  };
  // Login/logout swap the whole shell; drop any scroll position left over
  // from focusing the auth form so the staff view opens at its top.
  useEffect(() => { window.scrollTo(0, 0); }, [session !== null]);
  useEffect(() => {
    setSessionExpiredHandler(() => { setBanner("Your session expired. Please sign in again."); logout(); });
    return () => setSessionExpiredHandler(null);
  }, []);
  return session ? <AppShell session={session} onLogout={logoutAndRevoke} /> : <AuthScreen onAuthenticated={handleAuthenticated} banner={banner} />;
}


createRoot(document.getElementById("root")).render(<App />);